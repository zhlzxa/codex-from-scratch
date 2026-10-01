# 第 9 章 · 工具太多

> **代码**：`steps/step09_mcp/`
> **分支**：`feat/mcp`
> **产出**：Agent 能用不是它自己写的工具——来自配置文件里列出的 MCP server；名字不会撞，结果被整理成一个字符串，
> 几十个工具的说明不会悄悄占满整个请求
> **前置**：做完第 8 章。测试不联网（但会真的启动子进程）。探针 `probe_mcp.py` 有三段要真的请求模型（需要 `OPENAI_API_KEY`）；
> 没有 key 照着读正文里的输出即可。
> **这一章多一个依赖**：官方的 MCP SDK（`mcp>=2.1`）。
> **这一章很长**，可以分三次读：§1–§7 是"把别人的工具接进来"，§8–§15 是"别人的进程会出什么事"，之后是接进命令行和验证。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **MCP（Model Context Protocol）**：一种约定。一个独立的程序（叫 **MCP server**）按这个约定告诉你"我有哪些工具、每个工具要什么参数"，并接受你的调用。
  装一个 server，就多一批工具。
- **stdio**：标准输入、标准输出。这一章的 server 是我们启动的子进程，和它说话的方式是往它的标准输入写、从它的标准输出读。
- **stderr**：标准错误。程序报错时写东西的另一个通道，和标准输出分开。
- **SDK**：别人写好的、替你处理某个协议细节的库。
- **命名空间（namespace）**：给名字加前缀，让来自不同地方的同名东西不冲突。
- **content block**：MCP 的工具返回的不是一个字符串，而是一串"块"，每块有类型（文字、图片、资源……）。
- **环境变量**：操作系统里"名字 = 值"的一组设置，子进程默认会继承父进程的。API key 常常放在这里。
- **变异残留**：变异测试会故意把源码改坏；如果它被强行中断，改坏的那一处可能留在源码里。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `async with Client(...) as client:` | 异步版的 `with`：进入时建立连接，离开时关闭 |
| `contextlib.AsyncExitStack()` | 一个"记着要关哪些东西"的栈；可以先进入一些上下文，以后再统一关闭 |
| `asyncio.create_task(协程)` | 让一个协程在后台作为独立的任务运行 |
| `loop.create_future()` / `fut.set_result(x)` / `await fut` | Future：一个"以后会有结果"的占位。一边 `await` 它，另一边给它结果 |
| `asyncio.shield(fut)` | 保护 `fut`：外面的等待被取消时，`fut` 本身不被取消 |
| `asyncio.wait({task}, timeout=…)` | 等任务结束，最多等这么久；到时间不抛异常，也不取消任务 |
| `getattr(obj, "name", 默认)` / `hasattr(obj, "name")` | 按名字取属性（没有就给默认值）/ 问有没有这个属性 |
| `re.compile(r"[^a-zA-Z0-9_-]").sub("_", s)` | 用正则表达式把 `s` 里所有"不是字母、数字、下划线、连字符"的字符换成 `_` |
| `(name := 表达式)` | "海象运算符"：一边赋值，一边把值拿来用 |
| `tempfile.NamedTemporaryFile(...)` | 建一个有名字的临时文件 |
| `if TYPE_CHECKING:` | 这个 `if` 里的 import 只给类型检查器看，程序运行时不执行 |

### 0.3 开分支，加依赖

```bash
git switch main
git pull
git switch -c feat/mcp
uv add "mcp>=2.1"
```

---

## §1 这一章要做出来的东西

到第 8 章为止，Agent 有四个工具：`read_file`、`apply_patch`、`run_shell`、`request_permissions`。四个都是我们自己写的，
所以每一个问题都有我们说了算的答案——参数是什么形状、返回什么、碰了哪些文件、出错时说什么。

这一章要接上的东西，上面这些一条都不成立。装六个 MCP server，Agent 突然多出几十个工具：说明是别人写的，参数名是别人起的，什么时候崩溃是别人决定的。

---

## §2 定需求，猜故障

需求：

- 从一个配置文件启动若干 MCP server，把它们的工具交给模型；
- 模型调用这些工具，结果像自己的工具一样回到历史里；
- server 起不来、中途死掉，Agent 不能跟着垮；
- 第 8 章的调度器要知道这些工具能不能一起跑。

动工前的猜测清单，九条：

| 编号 | 猜测 | 怎么判断 |
|---|---|---|
| F09-01 | 两个 server 都有一个叫 `search` 的工具，撞名后崩溃 | 真的接两个 |
| F09-02 | 工具一多（六十个），模型选对工具的比例断崖式下降 | 真模型测 |
| F09-03 | 光是工具的说明就占掉几万 token | 量 |
| F09-04 | 一个启动慢的 server 拖住整个 Agent | 让一个 server 故意慢 |
| F09-05 | server 中途死了 | 让一个 server 故意死 |
| F09-06 | server 要用户授权（比如登录），流程卡住 | 让一个 server 反过来问问题 |
| F09-07 | 返回的形状五花八门 | 让一个 server 返回各种块 |
| F09-08 | 历史里提到的某个工具，现在没有了 | 构造 |
| F09-09 | 一个个调用太慢（二十次往返），应该批量 | 真模型测 |

先说结果里最出乎意料的三件：**F09-02 没有复现，而清单给它开的药（把工具藏起来、按需加载）量出来是负作用**；
**这一章的快照里一直留着一处变异测试的残留，把 API key 传给了每一个 server**；
**连着两个 server 时，重启其中一个会让 Agent 以为"被用户打断了"**。后两件是改写这一章时才发现的。

---

## §3 协议交给 SDK，其余的自己来

要和一个 MCP server 说话，先得有一个 MCP server。**自己写两个。** 借来的 server 没法让它在指定的时刻崩溃，而这一章要撞的故障里，
一半需要一个"按要求出故障"的 server：启动慢的、跑到一半死的、反过来问问题的、返回一堆奇怪的块的。

和 server 说话的那一边——**客户端**——不自己写协议，用官方的 SDK：

```python
from mcp import Client

async with Client(params) as client:
    tools = await client.list_tools()
    result = await client.call_tool("stat", {"name": "pyproject.toml"})
```

握手、版本协商、一条条消息怎么切分、请求编号怎么分配——一行都不用写。

> **这本书的分界线，在这里说清楚一次：教什么，就手写什么；不教的，就依赖它。**
> 从第 -1 章起我们就依赖 `httpx` 而不是自己写 HTTP，依赖 `pytest` 而不是自己写测试框架。这一章教的不是"消息怎么切分"，
> 而是：撞名怎么办、说明太多怎么办、结果的形状怎么统一、别人的进程死了怎么跟模型解释、子进程能看见什么、别人说的话信到什么程度。
> codex 也是这样：它用的是官方的 Rust 版 SDK。

`pyproject.toml` 的 `dependencies` 里多了一项（`uv add` 已经替你加了；注释是手写的）：

```toml
    # The second, and it arrived by deleting code rather than adding it.
    # Chapter 9 first hand-rolled JSON-RPC 2.0 over a subprocess's stdio -- a
    # framing loop, a pending-futures table, a request-id counter -- and that
    # was never what codex does: its workspace pins the official Rust SDK
    # (`rmcp = "=3.0.0"`, `codex-rs/Cargo.toml:393`).  Same rule as `httpx`
    # above: hand-roll what you are teaching, depend on what you are not.
    # This project is teaching tool-name collisions, schema budgets and
    # trusting somebody else's process -- not message framing.
    "mcp>=2.1",
```

### 3.1 第一个 server：管文件的

新建目录 `mcp_servers/`，新建 `mcp_servers/files_server.py`：

```python
"""A real MCP server, built on the official SDK, that can be told to misbehave.

This file used to hand-roll the *server* side of JSON-RPC the way
`minicodex/mcp.py` used to hand-roll the client side. Both were rewritten for
the same reason (see that module's docstring): codex pins the official SDK
(`rmcp = "=3.0.0"`, `codex-rs/Cargo.toml:393`), and framing was never what
chapter 9 is teaching.

Written rather than installed, though, and that part has not changed. codex
ships its own test server for exactly this reason
(`codex-rs/rmcp-client/src/bin/test_stdio_server.rs`): a test that borrows
somebody else's MCP server is also testing their server, and cannot ask it to
fail on cue. Every fault in this chapter needs a server that does one specific
wrong thing.

What the SDK does and does not let this server do is itself worth noticing.
The good behaviour -- initialize, capability exchange, tool listing, the
`isError` result shape -- is now three decorators. The *bad* behaviour is what
still needs code, and a couple of shapes are no longer reachable at all: the
SDK will not emit malformed JSON or a duplicate request id, because it does
not build the frames by hand any more either. Those cases moved to
`tests/test_faults_ch09.py`, which drives `normalise` directly and says so.

Environment variables, all optional, each one a fault this server reproduces:

  MCP_STARTUP_DELAY   seconds to sleep before serving (F09-04)
  MCP_DIE_AFTER       exit(1) after this many `tools/call` requests (F09-05)
  MCP_CRASH_AFTER     die with a traceback on stderr, after this many calls
  MCP_TOOL_PREFIX     prefix every tool name, to build a 60-tool server (F09-02)
  MCP_EXTRA_TOOLS     add N filler tools with realistic schemas (F09-02/F09-03)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer

ROOT = Path(os.environ.get("MCP_ROOT", ".")).resolve()
PREFIX = os.environ.get("MCP_TOOL_PREFIX", "")

server = MCPServer("files")
_calls = 0


def _budget() -> None:
    """Die on schedule, if this run was told to.

    Called at the top of every tool, because the two ways a server can leave
    are different faults: `sys.exit` is a clean disappearance with nothing on
    stderr, and an unhandled error leaves a traceback. A client that can only
    report the first has nothing to tell the user (F09-05).
    """
    global _calls
    _calls += 1
    die_after = int(os.environ.get("MCP_DIE_AFTER", "0"))
    if die_after and _calls > die_after:
        # `os._exit`, not `sys.exit`: inside the SDK's request handling a
        # `SystemExit` is just another exception and gets turned into a
        # perfectly polite error result. This fault needs the process gone.
        os._exit(1)
    crash_after = int(os.environ.get("MCP_CRASH_AFTER", "0"))
    if crash_after and _calls > crash_after:
        sys.stderr.write("MemoryError: index too large to load\n")
        sys.stderr.flush()
        os._exit(70)


async def _noise(ctx: Context) -> None:
    """Emit a perfectly legal log notification before answering.

    `MCP_LOG_NOISE` is how this chapter proves the pipe carries more than
    answers.  A server may send `notifications/message` whenever it likes, and
    the naive client this chapter opened with read that notification as the
    result of `initialize` -- every reply after it off by one, nothing raised.

    Keeping the switch after the SDK rewrite is the point: the *fault* did not
    go away, the *fix* moved.  Demultiplexing responses from notifications
    from server-initiated requests is now the SDK's job, and the test that
    used to prove our reader did it correctly now proves theirs does.
    """
    if os.environ.get("MCP_LOG_NOISE"):
        await ctx.info("handling a call")


@server.tool(name=f"{PREFIX}search")
async def search(query: str, ctx: Context) -> str:
    """Search the indexed files for a literal string."""
    await _noise(ctx)
    _budget()
    hits = [
        p.name
        for p in sorted(ROOT.glob("*"))
        if p.is_file() and query in p.read_text(encoding="utf-8", errors="replace")
    ]
    return "\n".join(hits) if hits else "no matches"


@server.tool(name=f"{PREFIX}stat", annotations={"readOnlyHint": True})
async def stat(name: str, ctx: Context) -> str:
    """Return the size in bytes of one indexed file."""
    await _noise(ctx)
    _budget()
    target = ROOT / name
    if not target.is_file():
        # Raising is what produces `isError: true` under the SDK, and it is
        # the shape chapter 0's rule needs: a failed tool is text the model
        # acts on, not a dead connection.
        raise FileNotFoundError(f"no such file: {target.name}")
    return str(target.stat().st_size)


def _add_filler(index: int) -> None:
    """One more tool with a realistic schema, for the budget measurements."""

    def _filler(target: str, dry_run: bool = False, limit: int = 0) -> str:
        _budget()
        return f"filler_{index:02d} ok"

    # The description is set here rather than as a docstring because an
    # f-string is not one: `f"""..."""` inside a function body is an
    # expression that is evaluated and discarded, so the tool would ship with
    # no description at all and the schema-budget measurements -- which are
    # about how much description costs -- would quietly measure nothing.
    _filler.__doc__ = (
        f"Filler tool number {index}. Performs operation {index} against the "
        "configured backend and returns a structured report of what it did."
    )
    server.tool(name=f"{PREFIX}filler_{index:02d}")(_filler)


for _index in range(int(os.environ.get("MCP_EXTRA_TOOLS", "0"))):
    _add_filler(_index)


if __name__ == "__main__":
    delay = float(os.environ.get("MCP_STARTUP_DELAY", "0"))
    if delay:
        # Before `run()`, so the client's startup deadline is what expires --
        # the server has not begun speaking the protocol at all yet, which is
        # exactly F09-04's shape.
        time.sleep(delay)
    server.run()
```

> - `server = MCPServer("files")`，然后用 `@server.tool(...)` 装饰一个函数，它就成了一个工具：函数的参数是工具的参数，docstring 是工具的说明。
>   `server.run()` 开始在标准输入输出上按 MCP 的约定收发消息。
> - 两个正经工具：**`search`**（哪些文件里有这个字符串）和 **`stat`**（一个文件多大）。`stat` 带着 `annotations={"readOnlyHint": True}`——
>   server 自称"这个工具只读"。§14 会用到。`stat` 找不到文件时 `raise`：在 SDK 里，工具里抛出的异常会变成一个"出错了"的结果，而不是把连接弄断。
> - **开头 docstring 里列的那些环境变量，每一个都是"让这个 server 出一种故障"的开关**：
>   `MCP_STARTUP_DELAY`（启动前先睡几秒）、`MCP_DIE_AFTER`（第 N 次调用之后悄悄退出）、`MCP_CRASH_AFTER`（第 N 次之后往 stderr 写一行再退出）、
>   `MCP_LOG_NOISE`（每次回答前先发一条日志通知）、`MCP_TOOL_PREFIX` 和 `MCP_EXTRA_TOOLS`（凑出很多工具）。
> - **`_budget()`**：每个工具开头都调用它；数调用次数，到了就"死"。用 `os._exit` 而不是 `sys.exit`：后者在 SDK 里只是又一个异常，会被客气地变成错误结果；这里要的是进程真的没了。
> - **`_add_filler(index)`**：造一个凑数的工具。注释解释了为什么说明要用 `__doc__ =` 来设：写成 `f"""..."""` 的不是 docstring，
>   工具会没有说明，而"说明要花多少 token"正是要量的东西。

### 3.2 第二个 server：管笔记的

新建 `mcp_servers/notes_server.py`：

```python
"""A second MCP server, which also happens to expose a tool called `search`.

That collision is the whole point (F09-01). Two servers written by two teams
who never met both pick the most obvious name for the most obvious tool, and
neither is wrong.

It also exposes the two shapes chapter 9 needs that `files_server.py` does not:

  - `render`: a result with an image block and an embedded resource block, so
    the normalising adapter has something other than plain text to normalise
    (F09-07).
  - `remember`: a tool that *writes*, is not read-only, and needs consent
    before it runs -- the server asks for it with an `elicitation/create`
    request of its own (F09-06).

Rewritten onto the official SDK along with everything else in this chapter;
see `minicodex/mcp.py`'s docstring for why.

**Why this is a subprocess and not an in-process server.** `mcp.Client` also
accepts a server *object* and speaks to it inside the calling process, which
is faster and still exercises the real protocol. It is not usable here for
two reasons, and both are this chapter's subject:

  - half of chapter 9's faults need a process that can **die** -- hang at
    startup, exit mid-call, leave a traceback on stderr. An in-process server
    cannot; it is the test.
  - that transport has **no back-channel**, so `remember`'s question to the
    client raises there regardless of protocol version:

        NoBackChannelError: Cannot send 'elicitation/create': this transport
        context has no back-channel for server-initiated requests.

The rule worth carrying away: **the in-process transport tests logic, not
processes.** Worth knowing before designing a suite around the fast option.

Environment variables:

  MCP_ELICIT      `remember` asks the client for confirmation before writing
  MCP_NOTES_DB    where `remember` writes (default: `notes.json` in cwd)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import (
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
    TextResourceContents,
)
from pydantic import BaseModel

NOTES = {
    "meeting": "Ship chapter 9 before the scheduler work goes stale.",
    "idea": "A tool nobody can find is a tool nobody calls.",
}

# A real 1x1 transparent PNG, so `render` returns an actual base64 image block
# rather than a made-up string that the adapter would never have to survive.
PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)
assert base64.b64decode(PIXEL).startswith(b"\x89PNG")

server = MCPServer("notes")


class Confirm(BaseModel):
    """The one-boolean schema chapter 5's approver can actually answer.

    `registry.elicitation_handler` declines anything wider than this on
    purpose: it can ask a human yes or no, and inventing a value to fill a
    richer schema would put made-up data into somebody else's system.
    """

    confirm: bool


@server.tool()
def search(query: str) -> str:
    """Search saved notes for a word."""
    needle = query.lower()
    hits = [f"{k}: {v}" for k, v in NOTES.items() if needle in k or needle in v.lower()]
    return "\n".join(hits) or "no notes match"


@server.tool()
def render(name: str) -> CallToolResult:
    """Render one note as text, an image and an embedded resource.

    Returns a whole `CallToolResult` rather than a list of blocks, because
    F09-07 needs `structuredContent` *alongside* the blocks and returning a
    list only fills in `content`.  A server that has both is the shape the
    normaliser has to survive.
    """
    return CallToolResult(
        content=[
            TextContent(type="text", text=f"note {name!r} rendered"),
            ImageContent(type="image", data=PIXEL, mimeType="image/png"),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=f"notes://{name}",
                    mimeType="text/plain",
                    text=NOTES.get(name, ""),
                ),
            ),
        ],
        structuredContent={"note": name, "bytes": len(NOTES.get(name, ""))},
    )


@server.tool()
async def remember(name: str, text: str, ctx: Context) -> str:
    """Save a note to disk. Not read-only."""
    if os.environ.get("MCP_ELICIT"):
        answer = await ctx.elicit(f"Save note {name!r} to disk?", schema=Confirm)
        accepted = answer.action == "accept" and getattr(answer.data, "confirm", False)
        if not accepted:
            # Raising rather than returning: `isError` is what says "this tool
            # ran and refused", and chapter 0's rule is that the model reads
            # that as information rather than as a broken connection.
            raise PermissionError("not saved: the user declined")

    # Through a thread: this is an `async def`, and blocking IO on the event
    # loop is the rule chapter 0 turned on and ruff's ASYNC240 enforces. The
    # same reason `_scratch` exists in the test file.
    await asyncio.to_thread(_save, name, text)
    return f"saved {name!r}"


def _save(name: str, text: str) -> None:
    path = Path(os.environ.get("MCP_NOTES_DB", "notes.json"))
    db = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    db[name] = text
    path.write_text(json.dumps(db), encoding="utf-8")


if __name__ == "__main__":
    server.run()
```

> - 它也有一个工具叫 **`search`**——搜笔记。**这个撞名是故意的**（§5）：两个互不相识的人写的两个 server，都给最显然的工具起了最显然的名字，谁都没错。
> - **`render`**：返回一段文字、一张图片（一个真的 1×1 像素的 PNG）、一个内嵌的资源，外加一份"结构化内容"。给 §9 用。
> - **`remember`**：会**写**磁盘的工具。设了 `MCP_ELICIT` 时，它写之前先反过来问客户端"要保存吗？"（`ctx.elicit`）。给 §13 用。
>   `Confirm` 是一个只有一个布尔字段的数据模型（`pydantic` 的 `BaseModel`，SDK 自带的依赖）。
> - 开头的 docstring 解释了为什么这些 server 是**子进程**，而不是在测试进程里直接运行的对象（SDK 支持后者，而且更快）：
>   这一章一半的故障需要一个**会死的进程**；在进程里直接运行的那个死不了，它就是你自己。

这两个文件会各写一个 `notes.json` 之类的东西出来，所以 `.gitignore` 加上：

```
# Written by the chapter 9 notes MCP server when `remember` is called without
# MCP_NOTES_DB.  Interlude A's lesson, one chapter later: the things that need
# ignoring are the ones a program creates by itself, not the ones you thought
# of while writing .gitignore.
notes.json
tests/_notes_*.json
```

> 第 0 章的教训在这里又用了一次：**需要忽略的，是程序自己会创建的东西。**

---

## §4 客户端：`mcp.py`

新建 `src/minicodex/mcp.py`。SDK 管协议；这个模块管 SDK **不管**的四件事——开头的 docstring 列了：子进程能看见哪些环境变量；启动和调用是**两个**超时；
工具失败不抛异常；死掉的 server 要能说出原因。整个文件：

```python
"""Talking to an MCP server, through the official SDK.

The protocol -- framing, `initialize`, version negotiation, telling a response
apart from a notification apart from a server-initiated request -- comes from
`mcp`, the official Python SDK. codex does the same with the Rust one
(`rmcp = { version = "=3.0.0" }`, `codex-rs/Cargo.toml:393`).

The line this book draws:

    **Hand-roll what you are teaching. Depend on what you are not.**

This project already depends on `httpx` rather than writing HTTP, on `pytest`
rather than writing a test runner. Chapter 9 is about tool-name collisions, a
schema budget, footprints, result normalisation, timeouts, and trusting
somebody else's process -- none of which is message framing. `registry.py`
holds most of that; this module holds the rest.

What is this module's own work, and why none of it is the SDK's:

  - **The environment allowlist.** The SDK ships one
    (`DEFAULT_INHERITED_ENV_VARS`), and it is not this one. Ours was arrived
    at by a real failure -- chapter 11's `SYSTEMROOT`/Winsock hang -- and
    keeping it is not stubbornness: an SDK's default is a reasonable guess
    about every caller, and a fault list is a fact about this one (F09-10).

  - **Two timeouts, not one.** A server that hangs during startup hangs the
    agent; a server that hangs during a call hangs a turn. Different failures,
    different budgets (F09-04).

  - **`call()` never raises for a failed tool.** Chapter 0's rule -- a tool's
    failure is information the model needs, not an exception that ends the
    session -- applies just as much when the failure happened in somebody
    else's process.

  - **A dead server has to be able to say why.** The SDK reports
    `Connection closed`; the model needs more than that, so stderr is still
    drained and kept (F09-05).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp import Client
from mcp.client.stdio import (
    DEFAULT_INHERITED_ENV_VARS,
    StdioServerParameters,
    stdio_client,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp_types import CallToolResult

# Long enough for a server that compiles something on first run, short enough
# that a broken one does not hold the agent hostage.  codex uses 30s for the
# same budget (`rmcp-client/src/rmcp_client.rs`).
DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_TOOL_TIMEOUT = 60.0

# How long to let the SDK take the child process down before giving up on it.
#
# Not a round number, and not a guess: `mcp.client.stdio` terminates in stages
# -- `PROCESS_TERMINATION_TIMEOUT = 2.0` for a polite exit, then
# `FORCE_KILL_TIMEOUT = 2.0` for the kill -- so a server that ignores both
# needs a shade over four seconds to be gone.
# A 5s cap is *nearly* enough, and that is worse
# than plainly too short: `asyncio.wait_for` cancels the cleanup it was
# waiting on, so a shutdown interrupted at second five leaves the child
# running and nothing left to kill it.  The test suite ended with ten orphaned
# Python processes and a pytest that would not exit -- F02-08 again, two
# chapters and one dependency later (F09-13).
SHUTDOWN_TIMEOUT = 15.0

# What an MCP subprocess is allowed to see.  Same allowlist idea as chapter 2's
# `ENV_ALLOWLIST` (F02-09) and for the same reason -- except an MCP server
# usually *does* need a credential, so `env` in the config adds to this
# explicitly, one variable at a time, rather than the process handing over
# everything it happens to have.
#
# The SDK has its own list and we do not use it (F09-10).  `mcp.client.stdio`
# exports `DEFAULT_INHERITED_ENV_VARS`:
#
#     APPDATA HOMEDRIVE HOMEPATH LOCALAPPDATA PATH PATHEXT
#     PROCESSOR_ARCHITECTURE SYSTEMDRIVE SYSTEMROOT TEMP USERNAME USERPROFILE
#
# Wider than ours on Windows (`USERNAME`, `USERPROFILE`, `TEMP`, `APPDATA` --
# four more ways for a server to learn who is running it and where their files
# are) and narrower on POSIX, where it has no `HOME`, no `LANG`, no `TZ`.
# Neither list is wrong; they answer different questions.  Theirs is a good
# guess about every caller they will ever have.  Ours is a fact about this one,
# and `SYSTEMROOT` is on it because chapter 11 watched `import asyncio` fail
# inside a subprocess without it.
#
# **And passing ours does not replace theirs.**  `mcp/client/stdio.py` builds
# the child's environment as
#
#     env=get_default_environment() | (server.env or {})
#
# -- a union.  Measured, with an allowlist of five variables actually present
# on this machine, the child received fourteen:
#
#     EXTRA: APPDATA HOMEDRIVE HOMEPATH LOCALAPPDATA PROCESSOR_ARCHITECTURE
#            SYSTEMDRIVE TEMP USERNAME USERPROFILE
#
# Nine variables this chapter had deliberately left out, arriving because a
# dependency had an opinion.  The boundary is bounded -- an unrelated secret
# in `os.environ` did *not* come through, because the union is with a fixed
# list rather than with the whole environment -- but "bounded" is not
# "chosen", and F02-09 was about choosing.
#
# So `_subprocess_env` blanks them (F09-10).  A key set to the empty string
# still arrives, and that is the best the union allows: the server learns that
# `USERNAME` exists and nothing about who it is.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "SYSTEMROOT", "PATHEXT")


class McpError(RuntimeError):
    """The server could not be reached, started, or spoken to.

    Distinct from a tool that ran and failed: that comes back as text for the
    model.  This one means the transport itself is not usable, which is a
    fact about the session rather than about the call.
    """


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT


@dataclass(frozen=True)
class RemoteTool:
    """One tool as the server describes it, before anything renames it.

    `name` is the raw MCP name and is what goes back on the wire.  The name the
    model sees is derived from this and lives in `registry.py`; keeping the two
    apart is F09-01's fix, and keeping the raw one is what makes a call still
    reach the right tool after the rename.

    `read_only` is the server's `annotations.readOnlyHint`, kept as
    `bool | None` rather than defaulting to False: "the server said it is not
    read-only" and "the server said nothing" are different states, and chapter
    8's scheduler is about to have to decide what to do with the difference.
    """

    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool | None = None


# Answering a server-initiated request.  Kept as a name because `registry.py`
# builds one (`elicitation_handler`) and because the shape -- params in, result
# out, raise to refuse -- is this project's, not the SDK's.  `_as_callback`
# below adapts it to what `mcp.Client` wants.
RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _subprocess_env(config: ServerConfig) -> dict[str, str]:
    """What the server process is allowed to see.

    Two passes, and the order between them is what makes the result an upper
    bound rather than a lower one.

    The comprehension is F02-09 one process further out: the allowlist decides
    what may be copied out of *this* process, because an MCP server is
    somebody else's program, started by us, with no use for the key this agent
    talks to its model with.

    The blanking pass is F09-10, and it exists only because the SDK unions its
    own `DEFAULT_INHERITED_ENV_VARS` into whatever it is given -- so an
    allowlist handed over as-is would be a floor, not a ceiling.  Naming each
    unwanted key with an empty value is the only override the union permits.
    """
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.update(config.env)
    for key in DEFAULT_INHERITED_ENV_VARS:
        if key not in env:
            env[key] = ""
    return env


def _as_callback(handler: RequestHandler) -> Any:
    """Wrap this project's `RequestHandler` as the SDK's elicitation callback.

    An adapter rather than a second implementation, because the
    thing that module knows -- that a server's question goes to chapter 5's
    approver, that a multi-field schema is declined rather than guessed at --
    is unaffected by which library carries the message.  Only the envelope
    differs.
    """
    from mcp_types import ElicitResult

    async def callback(context: Any, params: Any) -> Any:
        raw = {
            "message": getattr(params, "message", "") or "",
            "requestedSchema": getattr(params, "requestedSchema", None)
            or getattr(params, "requested_schema", None)
            or {},
        }
        result = await handler(raw)
        action = result.get("action", "decline")
        content = result.get("content")
        if action == "accept" and content is not None:
            return ElicitResult(action="accept", content=content)
        return ElicitResult(action=action)

    return callback


class McpClient:
    """One connection to one server.

    Not thread-safe and not meant to be: it belongs to one event loop.

    The lifecycle is still `start()` / `close()` rather than `async with`,
    and that is deliberate.  `mcp.Client` is an async context manager, which
    is the right shape for a script; a session holds several servers open
    across many turns and closes them when the *session* ends, not when a
    block exits.  The bridge is one task per connection (`_hold`): it enters
    the SDK's context, hands the connected client back, and waits to be told
    to leave.
    """

    def __init__(
        self,
        config: ServerConfig,
        *,
        handlers: dict[str, RequestHandler] | None = None,
        server: Any | None = None,
    ) -> None:
        self.config = config
        self.handlers = handlers or {}
        # `server` is the in-process escape hatch: `mcp.Client` accepts an
        # `MCPServer` object as readily as a command line, so the tests can
        # run a *real* server in this process with no subprocess and no
        # network.  Production never passes it.
        self._server = server
        self._client: Client | None = None
        # The task that holds the connection open, and how to ask it to stop.
        self._owner: asyncio.Task[None] | None = None
        self._closing: asyncio.Event | None = None
        self._stderr_path: Path | None = None
        # The last thing the server said before it stopped saying anything.
        self._last_words = ""
        self.server_info: dict[str, Any] = {}
        # Why the connection ended, for the message the model gets when it
        # calls a tool on a server that is no longer there.  A dead server that
        # cannot say why produces a support question nobody can answer.
        self.failure: str | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._client is not None

    def _target(self, stack: AsyncExitStack) -> Any:
        """What `mcp.Client` should connect to, and where stderr goes.

        The stderr detour is F09-05.  `Client` given a
        `StdioServerParameters` builds its own transport with
        `errlog=sys.stderr`, which sends a dying server's traceback to the
        terminal and leaves this client holding `Connection closed` -- true,
        and useless to a model that has to explain itself to a user.

        `Client` also accepts a `Transport`, and `stdio_client` takes an
        `errlog`, so the capture is an *argument* rather than a fork of the
        SDK.  A file and not a `StringIO`: `errlog` ends up as
        `anyio.open_process(stderr=...)`, which needs a real file descriptor.
        """
        if self._server is not None:
            return self._server

        # Not a `with`: the file has to outlive this function and stay open
        # for as long as the child writes to it.  The exit stack owns it.
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".mcp-stderr", delete=False
        )
        self._stderr_path = Path(handle.name)
        stack.callback(self._collect_last_words)
        stack.enter_context(handle)

        params = StdioServerParameters(
            command=self.config.command[0],
            args=list(self.config.command[1:]),
            env=_subprocess_env(self.config),
            cwd=self.config.cwd,
        )
        return stdio_client(params, errlog=handle)

    def _refresh_last_words(self) -> None:
        """Re-read the server's stderr without disturbing it.

        Called from `_why`, not only from the exit stack, and that is the
        whole of F09-05 on the live path: when a call fails with `Connection
        closed`, the connection is gone but the *stack is not unwound yet* --
        the session still holds this client open.  Reading only on close would
        put the explanation in a variable nobody looks at until after the
        message that needed it was already sent.
        """
        path = self._stderr_path
        if path is None:
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - the file was never created
            return
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            self._last_words = lines[-1][:400]

    def _collect_last_words(self) -> None:
        """Final read, then drop the file.  Runs on the exit stack."""
        self._refresh_last_words()
        path, self._stderr_path = self._stderr_path, None
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()

    async def start(self) -> None:
        """Connect to the server and complete the MCP handshake.

        The handshake itself is the SDK's -- version negotiation, capability
        exchange, `notifications/initialized` -- and none of it is written
        here any more.  What is written here is the deadline around it, which
        the SDK does not impose and which F09-04 says has to exist.
        """
        callbacks: dict[str, Any] = {}
        elicit = self.handlers.get("elicitation/create")
        if elicit is not None:
            callbacks["elicitation_callback"] = _as_callback(elicit)

        opened: asyncio.Future[Client] = asyncio.get_running_loop().create_future()
        closing = asyncio.Event()
        owner = asyncio.create_task(self._hold(callbacks, opened, closing))
        try:
            # `shield`, so that the deadline expiring cancels the *wait* and
            # not the future the owner task is about to resolve.
            client = await asyncio.wait_for(
                asyncio.shield(opened), timeout=self.config.startup_timeout
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            self.failure = (
                f"{self.config.name} did not answer initialize within "
                f"{self.config.startup_timeout:.0f}s"
            )
            await self._dismiss(owner)
            raise McpError(self.failure) from exc
        except Exception as exc:
            await self._dismiss(owner)
            # After the owner has finished, because its unwinding is what reads
            # stderr: a server that failed to start usually said why on the way
            # out, and the message without it is "could not start" and nothing
            # else.
            detail = f"{exc}"
            if self._last_words:
                detail = f"{detail}: {self._last_words}"
            self.failure = f"could not start {self.config.name}: {detail}"
            raise McpError(self.failure) from exc
        except BaseException:
            # Cancelled while starting (a Ctrl-C, say).  The owner must not be
            # left running with nobody to tell it to stop.
            owner.cancel()
            raise

        self._owner = owner
        self._closing = closing
        self._client = client
        info = getattr(client, "server_info", None)
        if info is not None:
            self.server_info = {
                "name": getattr(info, "name", "") or "",
                "version": getattr(info, "version", "") or "",
            }

    async def _hold(
        self,
        callbacks: dict[str, Any],
        opened: asyncio.Future[Client],
        closing: asyncio.Event,
    ) -> None:
        """Enter the connection, hand it over, and leave it -- all in one task.

        This is F09-17, and it was found with two servers connected.  The
        SDK's `Client` is built on anyio, whose cancel scopes belong to the
        task that entered them and must be left in the reverse of the order
        they were entered.  The first version entered every server's context
        from the caller's task and kept them on separate exit stacks -- so the
        session's task held files' scope, then notes' scope, and a restart of
        *files* tried to leave the outer one first.  Measured: the call that
        triggered the restart raised

            CancelledError: Cancelled via cancel scope ...

        in the agent's own task, which chapter 7 then reported to the model as
        "interrupted by the user" -- and every later await in that task was
        cancelled too.  One server never shows it: there is nothing to be out
        of order with.

        Giving each connection its own task removes the ordering question
        instead of answering it: a scope entered here is left here, whatever
        the other connections are doing.  It also makes the startup deadline
        honest on Python 3.10 and 3.11, where `asyncio.wait_for` runs what it
        is given in a helper task -- so a context entered inside it would have
        been entered in one task and left in another.
        """
        try:
            async with AsyncExitStack() as stack:
                target = self._target(stack)
                client = await stack.enter_async_context(
                    Client(target, read_timeout_seconds=self.config.tool_timeout, **callbacks)
                )
                opened.set_result(client)
                await closing.wait()
        except Exception as exc:
            # Reported through the future while `start()` is still waiting on
            # it; after that there is nobody to tell, and `close()` deals with
            # whatever state the connection was left in.
            if not opened.done():
                opened.set_exception(exc)

    async def _dismiss(self, owner: asyncio.Task[None]) -> None:
        """Stop an owner task that never became a connection, and wait for it.

        A failed `start()` has already decided what went wrong; nothing the
        tidying-up does afterwards is allowed to replace that diagnosis.
        """
        owner.cancel()
        await asyncio.wait({owner}, timeout=SHUTDOWN_TIMEOUT)

    async def close(self) -> None:
        """Shut the server down.

        Closing a subprocess transport cleanly -- stdin first so a well-behaved
        server exits on EOF, then a wait, then a kill, then the pipes themselves
        so the Windows proactor loop does not raise from `__del__` -- is the
        SDK's job, and a better place for it: that is subprocess plumbing, not
        anything about agents.
        """
        owner, self._owner = self._owner, None
        closing, self._closing = self._closing, None
        self._client = None
        if owner is None or closing is None:
            return
        # Asked, not cancelled: the owner leaves the SDK's context the ordinary
        # way, which is what lets the SDK take the child down in its own
        # stages.  `asyncio.wait` rather than `wait_for`, because `wait_for`
        # cancels what it was waiting on when the time is up -- and a cleanup
        # cancelled at second fifteen leaves the child running with nothing
        # left to kill it (F09-13).
        closing.set()
        done, _ = await asyncio.wait({owner}, timeout=SHUTDOWN_TIMEOUT)
        if not done:  # pragma: no cover - slow child
            # Said out loud rather than swallowed.  Past this point the child
            # is beyond this process's reach, and a session that leaks one
            # should be able to name it -- silence here is how ten of them
            # accumulate before anybody notices.
            self.failure = self.failure or (
                f"{self.config.name} did not shut down within "
                f"{SHUTDOWN_TIMEOUT:.0f}s and may still be running"
            )

    # -- the two things anyone actually calls --------------------------------

    async def list_tools(self) -> list[RemoteTool]:
        client = self._require()
        try:
            result = await asyncio.wait_for(client.list_tools(), timeout=self.config.tool_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            raise McpError(f"{self.config.name} did not answer tools/list in time") from None
        except Exception as exc:
            raise McpError(self._why(exc)) from exc

        tools: list[RemoteTool] = []
        for raw in result.tools:
            annotations = getattr(raw, "annotations", None)
            hint = getattr(annotations, "read_only_hint", None) if annotations else None
            tools.append(
                RemoteTool(
                    server=self.config.name,
                    name=raw.name,
                    description=str(getattr(raw, "description", "") or ""),
                    # SDK 2.x is snake_case on the Python side and still
                    # `inputSchema` on the wire.  Reading the attribute rather
                    # than the wire key is the point of using it.
                    input_schema=dict(raw.input_schema or {"type": "object", "properties": {}}),
                    read_only=hint if isinstance(hint, bool) else None,
                )
            )
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Invoke one tool.

        Raises only for transport failures, never for a tool that ran and
        reported an error -- that comes back in the result, with `is_error`
        set, and `registry.normalise` turns it into text the model can act on.
        """
        client = self._require()
        try:
            return await asyncio.wait_for(
                client.call_tool(name, arguments), timeout=self.config.tool_timeout
            )
        except (TimeoutError, asyncio.TimeoutError):
            raise McpError(
                f"{self.config.name}.{name} did not answer within {self.config.tool_timeout:.0f}s"
            ) from None
        except Exception as exc:
            raise McpError(self._why(exc)) from exc

    def _require(self) -> Client:
        client = self._client
        if client is None:
            raise McpError(self.failure or f"{self.config.name} is not running")
        return client

    def _why(self, exc: Exception) -> str:
        """The message the model gets when the transport failed, and the point
        at which this client admits it is no longer connected.

        The SDK says `Connection closed`, which is true and useless: it does
        not say whether the server crashed, was killed, or exited cleanly, and
        the traceback that would have said went to a pipe.  F09-05 is that
        gap, and it is still this module's to fill -- so the server's stderr
        is re-read here and appended.

        Dropping `_client` is the other half (F09-14).  There is no
        `returncode` to ask about -- the transport keeps the process handle --
        so liveness has to be *inferred* from a call failing, and the inference
        has to happen here: otherwise `registry`'s `if not client.alive` never
        fires and a dead server is never restarted.  "The client object still
        exists" is true of a client whose server died ten seconds ago.
        """
        self._refresh_last_words()
        self._client = None
        detail = f"{self.config.name}: {exc}"
        if self._last_words:
            return f"{detail}: {self._last_words}"
        return detail


__all__ = [
    "DEFAULT_STARTUP_TIMEOUT",
    "DEFAULT_TOOL_TIMEOUT",
    "ENV_ALLOWLIST",
    "McpClient",
    "McpError",
    "RemoteTool",
    "RequestHandler",
    "ServerConfig",
]
```

很长，一半是注释。分块看。

> **常量。**
> - `DEFAULT_STARTUP_TIMEOUT = 30`、`DEFAULT_TOOL_TIMEOUT = 60`：**两个预算，因为是两种故障**——启动时卡住，拖的是整个 Agent；调用时卡住，拖的是一轮。F09-04。
> - `SHUTDOWN_TIMEOUT = 15`：关闭时最多等多久。注释说了它为什么不是 5：SDK 自己分两个阶段结束子进程，各 2 秒；5 秒"差不多够"比"明显不够"更糟。
> - `ENV_ALLOWLIST`：子进程允许看见的环境变量。它上面那一大段注释是 §15 的故事。

> **`McpError`**：server 连不上、起不来、说不上话。和"工具运行了但失败了"是两回事——后者会变成给模型看的文字。

> **`ServerConfig`**：一个 server 的配置——名字、启动命令、额外的环境变量、工作目录、两个超时。
> **`RemoteTool`**：server 描述的一个工具。`name` 是 server 那边的原名（§5 讲为什么必须留着）；
> `read_only` 是 `bool | None`——"server 说它不是只读的"和"server 什么都没说"是两种状态。

> **`_subprocess_env(config)`**：§15。**`_as_callback(handler)`**：§13。

> **`McpClient`**：和一个 server 的一条连接。
>
> - **为什么是 `start()` / `close()`，而不是 `async with`**：SDK 的 `Client` 要用在 `async with` 里，这对一段脚本很合适；
>   但一次会话要同时开着好几个 server，跨很多轮，在**会话**结束时才关。
> - **`_target(stack)`**：决定连到哪里。正常情况是组装 `StdioServerParameters`（命令、参数、环境、目录），交给 `stdio_client`。
>   中间那段"临时文件"是 §10 的事：把 server 的 stderr 接到一个文件里。
> - **`start()`、`_hold()`、`_dismiss()`、`close()`** 这四个方法合起来做一件事：让连接由**它自己的一个任务**从头管到尾。
>   为什么这么绕，是 §11 的故事；现在先知道它们的分工——
>   `_hold` 是那个任务的全部内容：进入 SDK 的连接 → 把连好的客户端交出去（`opened.set_result`）→ 等"可以结束了"的信号（`closing.wait()`）→ 离开。
>   `start()` 启动这个任务，并等它交出客户端，**最多等 `startup_timeout` 秒**——SDK 不设期限，这个期限是我们加的。超时或出错，就让那个任务停下（`_dismiss`），抛 `McpError`。
>   `close()` 发出"可以结束了"的信号，最多等 `SHUTDOWN_TIMEOUT` 秒。
> - **`list_tools()`**：问 server 有哪些工具，变成一串 `RemoteTool`。SDK 给的对象用的是 Python 风格的属性名（`input_schema`、`read_only_hint`）。
> - **`call(name, arguments)`**：调用一个工具。**只在连接出问题时抛异常**；工具运行了但报告出错，是正常返回。
> - **`_require()`**：没连着就抛 `McpError`。
> - **`_why(exc)`**：连接出问题时，拼出给模型看的那句话——把 server 最后写在 stderr 上的话附上（§10）。它还做了另一件事：`self._client = None`。
>   SDK 不把子进程交给我们，所以没法直接问"它还活着吗"；**一次调用失败了，就推断它死了**。不这样做，`alive` 永远是 `True`，死掉的 server 永远不会被重启。

### 4.1 一个依赖就是一个断言

SDK 替我们做的事里，有一件值得专门验一下。那根管道上跑的不只是"你问一句、它答一句"：server 可以随时发**通知**（比如一条日志）。
一个"写一行、读一行"的客户端，会把那条日志当成上一个问题的回答，然后**每一个回答都往后错一格，从头到尾没有任何异常**。

把消息分清楚是 SDK 的事，所以这个故障不会发生。**但仍然为它留一个测试**。新建 `tests/test_faults_ch09.py`，开头和帮手：

```python
"""Chapter 9: many tools, from servers this process does not control.

Every test here starts real subprocesses and speaks the real protocol to them.
The servers live in `mcp_servers/` and are ours, which is the point: a test
that borrows somebody else's MCP server cannot ask it to fail on cue.

Nothing in this file touches the network.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent_types import ToolCall
from minicodex.approval import AllowAll, ApprovalReply, DenyAll, Session
from minicodex.mcp import McpClient, McpError, RemoteTool, ServerConfig
from minicodex.registry import (
    MAX_RESULT_CHARS,
    McpRegistry,
    connect,
    elicitation_handler,
    is_remote,
    load_config,
    model_name,
    normalise,
    route_footprint,
    sanitize,
)
from minicodex.scheduler import STATEFUL, Footprint, batches, conflicts
from minicodex.tokens import estimate_messages

SERVERS = Path(__file__).resolve().parent.parent / "mcp_servers"


def config(script: str, name: str, **env: str) -> ServerConfig:
    return ServerConfig(
        name=name,
        command=(sys.executable, str(SERVERS / script)),
        env=env,
        startup_timeout=20.0,
        tool_timeout=20.0,
    )


def files(name: str = "files", **env: str) -> ServerConfig:
    return config("files_server.py", name, **env)


def notes(name: str = "notes", **env: str) -> ServerConfig:
    return config("notes_server.py", name, **env)


def a_call(tool: str, **arguments: Any) -> ToolCall:
    """A `ToolCall` for a tool named `tool`.

    The parameter is not called `name`, which is what it was called first:
    `call("mcp__files__stat", name="a.py")` is a `TypeError`, because `name`
    is also the argument that tool takes.
    """
    return ToolCall("call_1", tool, arguments, json.dumps(arguments))


async def started(*configs: ServerConfig, **kwargs: Any) -> tuple[McpRegistry, list[McpClient]]:
    registry = McpRegistry(**kwargs.pop("registry_kwargs", {}))
    clients = await connect(list(configs), registry, **kwargs)
    return registry, clients


def _scratch(name: str) -> Path:
    """A file the notes server will write to, removed before and after.

    Built here rather than inline in the test because `Path.exists()` inside
    an `async def` is ruff's ASYNC240 -- blocking IO on the event loop, the
    same rule chapter 0 turned on and chapter 2 kept paying attention to.
    """
    path = Path(__file__).resolve().parent / name
    path.unlink(missing_ok=True)
    return path


async def stop(clients: list[McpClient]) -> None:
    for client in clients:
        await client.close()

```

> - `config(script, name, **env)`、`files(...)`、`notes(...)`：造出"启动那两个 server"的配置，可以带环境变量（也就是那些故障开关）。`sys.executable` 是当前这个 Python。
> - `a_call(tool, **arguments)`：造一个 `ToolCall`。docstring 记了一件小事：参数原来叫 `name`，结果和工具自己的 `name` 参数撞了。
> - `started(*configs)`：把这些 server 都连上，返回注册表（§5）和客户端们。`stop(clients)`：全部关掉。
> - `_scratch(name)`：一个测试用的文件路径，先删干净。

```python
async def test_a_notification_is_not_the_answer_to_the_last_request() -> None:
    """The fault the naive client had, still asserted after the SDK rewrite.

    `MCP_LOG_NOISE` makes the server emit a legal `notifications/message`
    before every reply.  A client that reads one line per request reads that
    notification as the result of `initialize`, and every answer after it is
    off by one -- with no exception raised anywhere.

    The *fix* moved and the *property* did not.  Demultiplexing responses from
    notifications from server-initiated requests used to be `_read_loop` in
    `mcp.py`; it is the SDK's job now.  The test stayed because a property
    worth two hundred lines of our own code is worth eight lines of assertion
    against somebody else's -- a dependency is a claim, and this is the claim
    being checked.
    """
    client = McpClient(files(MCP_LOG_NOISE="1"))
    await client.start()
    try:
        assert client.server_info.get("name") == "files"
        tools = {tool.name for tool in await client.list_tools()}
        assert tools == {"search", "stat"}
        assert normalise(await client.call("stat", {"name": "pyproject.toml"})).isdigit()
    finally:
        await client.close()


async def test_closing_a_client_leaves_no_process_behind() -> None:
    """F02-08's rule, asserted the only way the SDK still allows.

    This test used to reach for `client._proc` and check `returncode`.  The
    SDK owns the subprocess now and does not hand it out -- `stdio_client`
    yields streams and keeps the handle -- so the process is no longer
    directly observable from here (the same loss F09-15 records).

    What is left is observable and is what actually matters: after `close()`
    the client reports itself shut, and the temporary file its stderr was
    being captured into has been cleaned up.  That second assertion is the
    one with teeth: it can only be true if the exit stack really unwound,
    which is the thing that also terminates the child.
    """
    client = McpClient(files())
    await client.start()
    assert client.alive
    stderr_file = client._stderr_path
    assert stderr_file is not None and stderr_file.exists()

    await client.close()

    assert not client.alive
    assert not stderr_file.exists()
```

> - 第一个：打开 `MCP_LOG_NOISE`，server 每次回答前先发一条日志通知。工具列表和调用结果仍然是对的。
>   **"分得清通知和回答"现在是别人的承诺，而承诺要验。** 哪天 SDK 升级后行为变了，变红的是这个测试，而不是某个用户那里"工具表是空的"。
> - 第二个：关闭之后，客户端说自己已经断开，而且那个接 stderr 的临时文件被清理掉了——后一条只有在连接真的完整地退出时才会成立。

---

## §5 F09-01：两个 server 都叫 `search`

最直接的合并方式，是把所有工具放进一个字典，用工具名当键。这一章的探针脚本 `probe_mcp.py`（§17 给全文）的第一段把它固定了下来：

```
$ uv run python probe_mcp.py collide
=== F09-01: two servers, one table ===
    naive dict:      3 tools from 4 declared -> {'search': 'notes', 'stat': 'files', 'render': 'notes'}
    namespaced:      4 tools -> ['mcp__files__search', 'mcp__files__stat', 'mcp__notes__render', 'mcp__notes__search']
```

四个工具放进去，字典里只有三个。**它不崩溃。** `files` 的 `search` 被 `notes` 的 `search` 盖掉了，从此不存在。
然后模型问"哪个文件里有 hatchling"，得到一句格式完整、语气笃定的 `no notes match`——`pyproject.toml` 里当然有 `hatchling`，只是没人去看。

**F09-01 成立，但不是猜的那样（🔴 崩溃），而是 🟡 静默。**

修法一句话：**模型看到的名字里带上 server 的名字。** `mcp__files__search` 和 `mcp__notes__search`。

新建 `src/minicodex/registry.py`——"注册表"：把来自任意多个 server 的工具，变成程序其余部分已经认识的三样东西（给模型的说明、给循环的处理函数、给调度器的脚印）。
先是开头和几个起名字的函数：

```python
"""Where remote tools become tools this agent can offer, call and schedule.

`mcp.py` knows about subprocesses.  `tools.py` knows about this repository.
This module is the seam: it takes `RemoteTool`s from any number of servers and
produces the three things the rest of the program already understands --
a schema list for the model, a `{name: handler}` table for the loop, and a
`Footprint` for chapter 8's scheduler.

Four decisions here are the chapter, and each was measured before it was
written:

  - **Names are rewritten, calls are not.**  Two servers both call their tool
    `search`; merging them into one dict silently loses one of them and
    answers with the other (F09-01).  The model sees `mcp__notes__search`; the
    wire still sees `search`.
  - **Schemas are shown until they do not fit, then replaced by an index.**
    Sixty schemas cost more tokens than everything else in the request put
    together (F09-03), on every turn, forever.  But deferring them behind a
    `tool_search` is a token fix that *costs accuracy*: measured on the same
    sixty-one tools, showing every schema found the right one 3/3 and hiding
    them found it 0/6.  So the trigger is a token budget, not a tool count.
  - **Results are normalised.**  A server may answer with text, an image, an
    embedded resource, structured JSON, or a flag saying the whole thing
    failed (F09-07).  Chapter 0's contract is that a tool returns one string.
  - **A remote footprint is a promise, not a fact.**  Chapter 8 refused to let
    a tool guess what it touches; a server's `readOnlyHint` is exactly such a
    guess, made by somebody else's code (F08-06).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex.agent_types import ToolCall, ToolFn
from minicodex.approval import ApprovalRequest, Session
from minicodex.mcp import McpClient, McpError, RemoteTool, RequestHandler, ServerConfig
from minicodex.policy import Risk
from minicodex.scheduler import STATEFUL, Footprint, FootprintFn
from minicodex.tokens import estimate_messages
from minicodex.tool_errors import tool_error

# The model-visible name of every remote tool starts here, so that "is this
# tool remote?" is a string test anywhere in the program and does not need the
# registry to answer it.
PREFIX = "mcp"
DELIMITER = "__"

# Measured, not read: `probe_mcp.py names` sends real tool names to the real
# API and reads the 400s.  The pattern is `^[a-zA-Z0-9_-]+$` -- a dot, a space
# or a slash in a server name is a rejected *request*, not a mangled tool name,
# so `sanitize()` is load-bearing rather than tidy.  The length limit is 128,
# which is worth writing down because the number this constant was first given
# was 64, from memory, and 65 characters went through without complaint.
MAX_TOOL_NAME = 128

# How much of a remote result the model is allowed to see.  Chapter 2 clips
# shell output and chapter 6 clips history items; an MCP result reaches the
# history through neither, which is F06-09's hole in a new shape.
MAX_RESULT_CHARS = 20_000

# How many tools `tool_search` returns when the model does not say.
DEFAULT_SEARCH_LIMIT = 5

# When the rendered schemas cost more than this, they are replaced by an index
# and a `tool_search`.  A **token** budget rather than a tool count, and that
# is the whole finding of this chapter's largest measurement: with sixty-one
# realistic tools, showing every schema found the right one 3/3 while deferring
# them behind a search found it 0/6.  Deferring is not an accuracy improvement
# to reach for at some tool count; it is what you do when the schemas will not
# fit, and it costs something every time.
#
# 4000 is a budget, not a cliff: it is what this project is willing to spend on
# tool schemas out of a small model's window, on every single turn.  A larger
# window can afford a larger number, which is exactly why it is a constructor
# argument.
DEFAULT_SCHEMA_BUDGET = 4000

_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize(part: str) -> str:
    """Make one name component legal as part of a function name.

    Lossy on purpose, and the loss is why `RemoteTool.name` is kept: `a.b` and
    `a b` both become `a_b`, so the sanitized name cannot be turned back into
    the name the server knows.  Only the forward direction is ever needed.
    """
    return _UNSAFE.sub("_", part)


def model_name(server: str, tool: str) -> str:
    return f"{PREFIX}{DELIMITER}{sanitize(server)}{DELIMITER}{sanitize(tool)}"


def is_remote(name: str) -> bool:
    return name.startswith(f"{PREFIX}{DELIMITER}")


@dataclass(frozen=True)
class Registration:
    """One remote tool, under the name the model will use for it."""

    model_name: str
    tool: RemoteTool
    client: McpClient

```

> - 开头的 docstring 列了这个模块的四个决定，后面各有一节。
> - `PREFIX`、`DELIMITER`：远程工具的名字都以 `mcp__` 开头。所以**"这是不是远程工具"只要看名字的开头**（`is_remote`），不需要去问注册表。
> - **`MAX_TOOL_NAME = 128`**、**`sanitize`**：下面 §5.1。
> - **`model_name(server, tool)`**：拼出模型看到的名字。
> - **`Registration`**：一个远程工具的登记——模型看到的名字、server 给的原始描述、它属于哪个客户端。

**必须想清楚的一点：改的只是模型看到的名字，发给 server 的名字一个字都不能变。** server 认识的是 `search`，把 `mcp__notes__search` 发过去它只会说不认识。
所以两个名字**分开存**：`RemoteTool.name` 是原名，`Registration.model_name` 是改过的名字。

### 5.1 名字里能有什么字符，是服务商说了算

`sanitize()` 把"不合法"的字符换成下划线。哪些字符不合法？不是"看起来奇怪的"，而是**模型服务商拒绝的**。探针的第二段把各种名字发给真的 API，看它怎么回答（2026-10-01，gpt-4o-mini）：

```
$ uv run python probe_mcp.py names
=== tool-name limits, measured against the real API ===
    64 chars             accepted, model called 'aaaa…'
    65 chars             accepted, model called 'aaaa…'
    128 chars            accepted, model called 'aaaa…'
    256 chars            HTTP 400: Invalid 'tools[0].function.name': string too long. Expected a string with maximum length 128, but got a string with length 256 instead.
    512 chars            HTTP 400: Invalid 'tools[0].function.name': string too long. Expected a string with maximum length 128, but got a string with length 512 instead.
    dots                 HTTP 400: Invalid 'tools[0].function.name': string does not match pattern. Expected a string that matches the pattern '^[a-zA-Z0-9_-]+$'.
    spaces               HTTP 400: Invalid 'tools[0].function.name': string does not match pattern. Expected a string that matches the pattern '^[a-zA-Z0-9_-]+$'.
    slash                HTTP 400: Invalid 'tools[0].function.name': string does not match pattern. Expected a string that matches the pattern '^[a-zA-Z0-9_-]+$'.
    double underscore    accepted, model called 'mcp__notes__search'
```

三件事：

1. **`sanitize()` 是必须的，不是讲究。** server 名字里带一个点，不是"这个工具的名字有点丑"，而是**整个请求被拒绝**——这一轮里所有的工具，
   包括 `read_file` 和 `apply_patch`，一起没了。**别人的 server 的名字，能让你自己的工具失效。**
2. **长度上限是 128，不是 64。** 这个常量第一次写的时候凭印象写了 64；65 个字符测下来毫无问题。常量上面的注释记着这件事。
3. **连字符 `-` 是合法的**，`sanitize` 不碰它。

`sanitize()` 会丢信息：`a.b` 和 `a b` 都变成 `a_b`。两个不同的原名可能变成同一个模型名。这时是"后来的盖掉先来的"还是"拒绝后来的"？
**拒绝**——盖掉就是 F09-01 本身，只是这次动手的是我们自己。这段逻辑在注册表的 `add` 里：

```python
class McpRegistry:
    """Every remote tool this session has, and what the model may see of them.

    Owns `visible`, the list of schemas the model is shown.  That list is
    handed to the model client *by reference* and mutated in place when
    `tool_search` reveals something: the client reads it when it builds each
    request, so a tool revealed on turn 3 is callable on turn 4 without
    anything having to rebuild anything.  Handing over a copy compiles, runs,
    and silently defeats the entire mechanism -- there is a test named after
    that mistake.
    """

    def __init__(
        self,
        *,
        schema_budget: int = DEFAULT_SCHEMA_BUDGET,
        local: list[dict[str, Any]] | None = None,
    ) -> None:
        self.schema_budget = schema_budget
        # This project's own tools, always shown and never deferred.  They are
        # in here rather than concatenated by the caller so that `visible` is
        # the *whole* tool list -- one object, handed to the model client once,
        # correct on every turn.  Two lists that have to be concatenated at
        # each call site is how one of them gets forgotten.
        self.local = list(local or [])
        self.registrations: dict[str, Registration] = {}
        self.visible: list[dict[str, Any]] = list(self.local)
        self.deferred: set[str] = set()
        # Servers that were configured and did not come up, kept so that the
        # failure can be reported once at startup and again -- with the reason
        # -- if the model asks for something that server would have provided.
        self.failures: dict[str, str] = {}
        # Model-visible name -> why that tool is not registered, so a call to
        # a tool that no longer exists can say what happened to it (F09-08).
        self.retired: dict[str, str] = {}

    # -- building it ---------------------------------------------------------

    def add(self, client: McpClient, tools: list[RemoteTool]) -> list[str]:
        """Register one server's tools.  Returns the names that were added."""
        added: list[str] = []
        for tool in tools:
            name = model_name(tool.server, tool.name)
            if len(name) > MAX_TOOL_NAME:
                # Refused rather than truncated: truncation is how two distinct
                # tools become one name, which is F09-01 again with the
                # registry as the culprit instead of the servers.
                self.retired[name[:MAX_TOOL_NAME]] = (
                    f"{tool.server}/{tool.name} was not registered: its name is "
                    f"{len(name)} characters and the limit is {MAX_TOOL_NAME}"
                )
                continue
            if name in self.registrations:
                # Same server, same sanitized name, different raw names --
                # `a.b` and `a b` both sanitize to `a_b`.  Same rule as above.
                self.retired[name] = (
                    f"{tool.server}/{tool.name} was not registered: its name collides "
                    f"with {self.registrations[name].tool.name} after sanitising"
                )
                continue
            self.registrations[name] = Registration(name, tool, client)
            added.append(name)
        self._restage()
        return added
```

> - **`McpRegistry`**：这次会话拥有的全部远程工具，以及模型能看到其中的什么。
>   - `local`：我们自己的四个工具的说明，也放进来（为什么，§7.3）。
>   - `registrations`：模型名 → 登记。`visible`：**给模型看的那份说明列表**。`deferred`：被藏起来、只给了名字的工具（§7）。
>   - `failures`：没起来的 server 和原因。`retired`：不在了的工具名和原因（§12）。
> - **`add(client, tools)`**：登记一个 server 的工具。名字太长：不登记，记下原因——**不截断**，截断正是两个工具变成一个名字的办法。
>   名字撞了：不登记，记下原因。最后调用 `_restage()` 重新决定展示什么（§7）。

```python
async def test_F09_01_a_flat_table_loses_a_tool_and_answers_with_the_other() -> None:
    """The naive merge, reproduced: not a crash, a wrong answer.

    The catalogue predicted a collision would announce itself.  It does not.
    One `search` overwrites the other, the model is offered three tools where
    four were declared, and asking for a *file* returns an answer about
    *notes* -- correctly formatted, confidently wrong.
    """
    registry, clients = await started(files(), notes())
    try:
        flat: dict[str, tuple[McpClient, RemoteTool]] = {}
        for registration in registry.registrations.values():
            flat[registration.tool.name] = (registration.client, registration.tool)

        assert len(registry.registrations) == 5
        assert len(flat) == 4  # `search` declared twice, kept once
        client, tool = flat["search"]
        assert tool.server == "notes"

        answer = normalise(await client.call("search", {"query": "hatchling"}))
        assert answer == "no notes match"  # the file containing it was never looked at
    finally:
        await stop(clients)


async def test_F09_01_namespacing_keeps_both_and_routes_each_to_its_own_server() -> None:
    registry, clients = await started(files(), notes())
    try:
        handlers = registry.handlers()
        assert set(handlers) == {
            "mcp__files__search",
            "mcp__files__stat",
            "mcp__notes__search",
            "mcp__notes__render",
            "mcp__notes__remember",
        }
        from_files = await handlers["mcp__files__search"]({"query": "hatchling"})
        from_notes = await handlers["mcp__notes__search"]({"query": "idea"})
        assert "pyproject.toml" in from_files
        assert "A tool nobody can find" in from_notes
    finally:
        await stop(clients)


def test_F09_01_the_wire_name_is_not_the_model_name() -> None:
    """Renaming for the model must not rename what goes to the server."""
    assert model_name("notes", "search") == "mcp__notes__search"
    assert is_remote("mcp__notes__search")
    assert not is_remote("read_file")


def test_F09_01_names_are_sanitised_because_the_provider_rejects_the_request() -> None:
    """Measured against the real API: the pattern is `^[a-zA-Z0-9_-]+$`.

    A dot in a server name is not a cosmetic problem.  It is an HTTP 400 for
    the whole request, so every tool in the session stops working, including
    the ones that have nothing to do with MCP.
    """
    assert sanitize("server.one") == "server_one"
    assert sanitize("my server/v2") == "my_server_v2"
    assert model_name("server.one", "tool.two-three") == "mcp__server_one__tool_two-three"
    for character in ".", " ", "/", "@":
        assert character not in model_name(f"a{character}b", f"c{character}d")


def test_F09_01_two_raw_names_that_sanitise_to_one_do_not_silently_merge() -> None:
    """`a.b` and `a b` both become `a_b`; the second is refused, not blended.

    The first version of this test used `a.b` and `a-b`, which do *not*
    collide -- the measured pattern allows a hyphen, so `sanitize` leaves it
    alone.  Worth keeping the correction visible: the set of characters that
    have to be replaced is the one the provider rejects, not the one that
    looks unusual.
    """
    registry = McpRegistry()
    registry.add(
        None,  # type: ignore[arg-type]
        [
            RemoteTool("s", "a.b", "first", {"type": "object"}),
            RemoteTool("s", "a b", "second", {"type": "object"}),
        ],
    )
    assert list(registry.registrations) == ["mcp__s__a_b"]
    assert registry.registrations["mcp__s__a_b"].tool.description == "first"
    assert "collides" in registry.retired["mcp__s__a_b"]
    assert model_name("s", "a-b") == "mcp__s__a-b"  # a hyphen is legal, left alone
```

> - 第一个把那个"朴素的合并"重现出来：五个工具进字典只剩四个，`search` 属于 `notes`，问文件得到 `no notes match`。
> - 第二个：带上命名空间后五个都在，各自到达自己的 server。
> - 第三、四个：名字的拼法；点、空格、斜杠、`@` 都不会出现在模型名里。
> - 第五个：`a.b` 和 `a b` 撞成 `a_b`，第二个被拒绝并记下原因。docstring 记着一次纠正：这个测试的第一版用的是 `a.b` 和 `a-b`，
>   它们**不会**撞——**要换掉的是服务商拒绝的字符，不是看起来不寻常的字符。**

```bash
git add pyproject.toml uv.lock .gitignore mcp_servers/ src/minicodex/mcp.py src/minicodex/registry.py tests/test_faults_ch09.py
git commit -m "feat(mcp): connect to MCP servers through the SDK, and namespace their tools"
```

---

## §6 F09-03：几十个工具要花多少 token

第 6 章发现过：工具的说明（schema）不在对话历史里，但**每一个请求都带着它**，压缩历史也不会让它变小。探针的第三段，用第 6 章那把尺子量（不需要联网）：

```
$ uv run python probe_mcp.py tokens
=== F09-03: schema cost, measured with chapter 6's estimator ===
    conversation alone:                           32 tokens
    +   4 tool schemas:    545 tokens  (94.5% of the request, 136 per tool)
    +  12 tool schemas:   1635 tokens  (98.1% of the request, 136 per tool)
    +  30 tool schemas:   4092 tokens  (99.2% of the request, 136 per tool)
    +  60 tool schemas:   8187 tokens  (99.6% of the request, 136 per tool)
    + 120 tool schemas:  16387 tokens  (99.8% of the request, 137 per tool)
    deferred (1 search tool only):                68 tokens
    Every number above is paid on every turn, whether or not a tool is used.
```

一个工具大约 136 个 token。六十个工具 8187 个，占一个短请求的 99.6%。

清单猜的是"几万 token"；按这种工具的大小，要两百多个工具才到三万。数字没对上，结论不受影响：
**这笔钱每一轮都付，用不用都付，而第 6 章的压缩对它完全无能为力。F09-03 成立。**

常见的解法是"两级加载"：先只给模型工具的名字和一句话说明，需要时再把完整的说明拿出来。量一下能省多少（六十一个工具）：

```
61 full schemas:            7108 tokens
61 name + one-line index:   1206 tokens   (17% of full)
61 names only:              432 tokens
```

只给名字和一句话，是全量的 17%。到这里，一切都很顺。

---

## §7 F09-02：把工具藏起来，会怎么样

清单上 F09-02 说：工具一多，模型选对工具的比例会断崖式下降。解法：按需暴露。§6 刚证明按需暴露省 token，两件事看起来是一件事：藏起来，又省又准。

这一节在原来写的时候错了三次，三次都记在这里，因为错法比结论更值得学。

### 7.1 第一轮：全对，但什么都没测到

造六十个假工具，每个叫 `operation_NN`，说明是"对后端执行第 N 号操作"。再放一个真正要找的：`mcp__notes__search`，"按关键词搜索保存的笔记"。任务："找到关于会议的那条笔记，用一个工具。"

61 个工具，3 次里 3 次选对。**这个测量什么也没测出来**：六十个干扰项里没有一个和笔记、搜索、会议有任何关系，模型的任务不是"在相近的工具里挑对的"，
而是"挑出唯一一个相关的"。第 4 章犯过同样的错——用一个 20 行的文件去测"模型会不会偷偷删代码"。

### 7.2 第二轮：复现了——然后发现是自己造出来的

第二轮把干扰项都做成**貌似能回答同一个问题**的工具：`search_documents`、`find_meeting`、`query_notes_index`……十个，每个复制六份凑够六十。
结果：9 个工具时就只有 0/3 了。看起来漂亮地复现了。

然后去看"藏起来"那一组为什么也是 0，把它搜到的东西打印出来：五个候选，**是同一个工具的五份复制品**。
那份假目录把十个概念各复制了六份，搜索取前五，五个名额全被同一个概念占满。**第二轮的"复现"，至少有一部分是那份目录造出来的。**

### 7.3 第三轮：六十个各不相同的工具

换成六个 server、每个十个工具、六十个全不重样——文档、日历、记忆、聊天、代码、维基，一台真的配了六个 MCP server 的机器的样子。
里面有 `search_documents`、`query_notes_index`、`search_wiki`、`recall_memory` 这些明摆着抢生意的邻居。这是现在探针里的版本，今天重跑（gpt-4o-mini，每组 3 次）：

```
$ uv run python probe_mcp.py selection
=== F09-02: finding one tool among N (gpt-4o-mini, 3 samples each) ===
    --- distractors: irrelevant ---
        1 tools offered:  correct 3/3
        9 tools offered:  correct 3/3
       31 tools offered:  correct 3/3
       61 tools offered:  correct 3/3
    --- distractors: plausible near-misses ---
        1 tools offered:  correct 3/3
        9 tools offered:  correct 3/3
       31 tools offered:  correct 3/3
       61 tools offered:  correct 3/3
    --- 61 hard tools, needle carries a chapter-3 disambiguating clause ---
        9 tools offered:  correct 3/3
       61 tools offered:  correct 3/3
    --- the same 61 hard tools, deferred behind tool_search ---
      query='meeting'                                    revealed 5 -> (no tool call)
      query='meeting'                                    revealed 5 -> (no tool call)
      query='meeting'                                    revealed 5 -> (no tool call)
      searched 3/3, then correct 0/3
```

**把说明全部给模型看：每一组都是 3/3。F09-02 没有复现。**

**把它们藏在一个"搜索工具"后面：0/3。** 模型 3 次都去搜了——搜的词是 `meeting`，从**任务**里挑出来的那个显眼的词。
而要找的那个工具的说明是"按关键词搜索保存的笔记并返回匹配的笔记"，里面根本没有 `meeting`。搜出来五个带 `meeting` 的工具，没有一个对；然后模型**没有再调用任何工具**。

原始记录里还试过三种补救：让它像真的 Agent 那样最多跑四轮（0/6，每次都以"能不能说得具体一点"结束——按第 0 章的规则，没有工具调用就是这次运行结束，什么也没做成）；
把没加载的工具的名字和一句话说明放进搜索工具自己的说明里；在搜索结果后面加一句"如果都不对，换个词再搜，不要将就"。后两样现在都在代码里，但**没有一样把 0 拉回来**。

> **一条故障"复现"了的时候，先问：复现的是它，还是你的探针。** 这一条用三种办法去撞，前两种都给出了想要的答案。

### 7.4 结论：两级加载是省 token 的办法，不是提高准确率的办法

所以代码里的开关，是 **token 预算**，不是工具数量：**装得下就全部展示，装不下才退到索引。** 这和第 6 章的压缩是同一类东西——不是优化，是不得已。

```python
    def _restage(self, defer: bool | None = None) -> None:
        """Decide what is shown in full and what is shown only by name.

        `defer` pins the answer instead of computing it.  Only `reconnect`
        passes it, to keep a running session in the mode it started in.

        `visible` is mutated in place rather than rebound, because the model
        client holds a reference to this exact list object.

        The `tool_search` schema is rebuilt every time the deferred set
        changes -- its description carries the index of what is still hidden,
        so the index cannot go stale while the list it describes does not.
        """
        names = sorted(self.registrations)
        schemas = [self.schema_for(name) for name in names]
        # All or nothing, not a greedy fill.  A partial list is the worst of
        # both: the model pays for schemas *and* has to know that what it can
        # see is not everything, and which half it got is decided by
        # alphabetical order, which means nothing to anybody.  The budget is
        # measured against the whole list the model receives, this project's
        # own tools included -- they are not free either.
        if defer is None:
            fits = estimate_messages((), self.local + schemas) <= self.schema_budget
            defer = bool(names) and not fits
        if not defer or not names:
            self.deferred = set()
            self.visible[:] = self.local + schemas
        else:
            self.deferred = set(names)
            self.visible[:] = [*self.local, self.search_schema()]

    def reveal(self, names: list[str]) -> list[str]:
        """Move tools from name-only to full schema in the model's tool list."""
        revealed = []
        for name in names:
            if name in self.deferred:
                self.deferred.discard(name)
                self.visible.append(self.schema_for(name))
                revealed.append(name)
        if revealed:
            for index, schema in enumerate(self.visible):
                if schema["function"]["name"] == "tool_search":
                    self.visible[index] = self.search_schema()
                    break
        return revealed

    def schema_for(self, name: str) -> dict[str, Any]:
        registration = self.registrations[name]
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": registration.tool.description,
                "parameters": registration.tool.input_schema,
            },
        }
```

> - **`_restage()`**：决定哪些完整展示、哪些只给名字。把所有工具的说明算一遍大小（**连我们自己的四个工具一起算**——它们也不是免费的）：
>   没超预算，全部展示；超了，**全部**藏起来，`visible` 里只留自己的工具加一个 `tool_search`。
>   - **要么全给，要么全藏，不"能塞多少塞多少"。** 给一半是两头不讨好：token 照付，模型还得知道"我看见的不是全部"，而它看见的是哪一半由字母顺序决定。
>   - `self.visible[:] = ...`：**原地**替换列表的内容，而不是让 `visible` 指向一个新列表。为什么这很要紧，§7.5。
>   - `defer` 参数是给 §11.2 的重启用的，平时是 `None`（自己算）。
> - **`reveal(names)`**：把几个工具从"只有名字"变成"完整说明"——往 `visible` 里追加，并把 `tool_search` 的说明更新一下（它里面列着"还没加载的"）。
> - **`schema_for(name)`**：一个工具给模型看的说明。

搜索工具本身：

```python
    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> str:
        """Rank the deferred tools against a query and reveal the best ones.

        Substring scoring over name and description, not embeddings: the
        catalogue is tens of tools, the query is the model's own words, and
        every extra moving part here is a thing that can be subtly wrong in a
        way nobody notices.  codex's own `tool_search` is the same idea with a
        larger catalogue behind it.
        """
        terms = [term for term in re.split(r"[^a-zA-Z0-9]+", query.lower()) if term]
        scored: list[tuple[int, str]] = []
        for name in sorted(self.deferred):
            registration = self.registrations[name]
            haystack = f"{name} {registration.tool.description}".lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((-score, name))
        scored.sort()
        chosen = [name for _, name in scored[: max(1, limit)]]
        remaining = len(self.deferred) - len(chosen)
        if not chosen:
            return (
                f"No tool matched {query!r}. {len(self.deferred)} tools are still "
                "unloaded; their names and descriptions are in this tool's own "
                "description. Search again using words from that list."
            )
        self.reveal(chosen)
        rendered = "\n".join(
            json.dumps({"name": name, **self.schema_for(name)["function"]}, ensure_ascii=False)
            for name in chosen
        )
        # The last sentence is not decoration.  Measured: given only the
        # matches, the model treats a near-miss as the best available answer
        # and stops -- 0/3, every run ending in "could you narrow this down"
        # while the tool it wanted sat unloaded.  A search result is a prompt,
        # and this one has to say what to do when the results are wrong
        # (chapter 3, F03-07).
        return (
            f"{len(chosen)} tool(s) loaded and now callable:\n"
            f"<functions>\n{rendered}\n</functions>\n"
            f"{remaining} other tool(s) are still unloaded. If none of the above is "
            "right for the task, call tool_search again with different words from "
            "the list in its description -- do not settle for a tool that is only "
            "approximately right, and do not ask the user for something the "
            "unloaded tools could find."
        )

    def index(self) -> str:
        """Every deferred tool, one line each: the name and its first sentence.

        This is the half of two-stage loading that the first version left out,
        and leaving it out was measured: with nothing but a search box, the
        model wrote a query from the *task* ("meeting") rather than from what
        exists, got five tools that matched that word, and never saw the one
        it needed -- 0/6, and in every one of those runs it then asked the user
        for help rather than searching again.

        The names are not free.  Measured on the sixty-one-tool catalogue: full
        schemas 7108 tokens, this index 1206, names alone 432.  The one-line
        descriptions are two thirds of the cost of the index and are what
        makes a query possible at all, so they stay.
        """
        lines = []
        for name in sorted(self.deferred):
            description = self.registrations[name].tool.description.strip()
            first = description.split(". ")[0].rstrip(".")
            lines.append(f"- {name}: {first}" if first else f"- {name}")
        return "\n".join(lines)

    def search_schema(self) -> dict[str, Any]:
        sources = sorted({r.tool.server for r in self.registrations.values()})
        return {
            "type": "function",
            "function": {
                "name": "tool_search",
                "description": (
                    "Load the full parameter schema for tools that are listed below by "
                    "name only, so that you can call them. You cannot call a tool from "
                    "this list until you have loaded it. Sources connected: "
                    f"{', '.join(sources) or '(none)'}.\n\n"
                    f"Tools available but not yet loaded:\n{self.index()}"
                ),
                "parameters": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Words from the names or descriptions listed above. "
                                "Example: notes search keyword"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"How many tools to reveal. Defaults to {DEFAULT_SEARCH_LIMIT}."
                            ),
                        },
                    },
                },
            },
        }

    def search_handler(self) -> ToolFn:
        async def handler(arguments: dict[str, Any]) -> str:
            query = arguments.get("query")
            if not isinstance(query, str):
                return tool_error(
                    'tool_search needs a "query" argument, a string',
                    you_sent=repr(arguments.get("query")),
                    do_this='Example: {"query": "search saved notes by keyword"}',
                )
            limit = arguments.get("limit")
            return self.search(query, limit if isinstance(limit, int) else DEFAULT_SEARCH_LIMIT)

        return handler
```

> - **`search(query, limit)`**：把查询拆成词，数每个没加载的工具的"名字 + 说明"里出现了几个词，取分最高的几个，加载它们，返回它们的完整说明。
>   不用更复杂的办法：目录只有几十个工具，每多一个零件就多一个出错了也没人发现的地方。
>   一个都没匹配上时，返回的话告诉模型**下一步怎么办**（去看这个工具自己的说明里的列表，换词再搜）；有匹配时，结尾那句"还有 N 个没加载，都不对就再搜，不要将就，也不要去问用户"也是同样的用意。
> - **`index()`**：每个没加载的工具一行——名字和说明的第一句。
> - **`search_schema()`**：`tool_search` 自己的说明，里面带着上面那份索引。
> - **`search_handler()`**：`tool_search` 的处理函数；参数不对时返回第 3 章那种三段式的错误。

### 7.5 那个列表是"按引用"交出去的

`tool_search` 加载一个工具的办法，是往 `registry.visible` 这个列表里追加。而模型客户端每次组装请求时读的，**就是这同一个列表对象**——§16 会看到，交给它的是 `tools=registry.visible`。
所以第 3 轮加载的工具，第 4 轮就能调用，什么都不用重建。

**写成 `tools=list(registry.visible)`（交一份拷贝）会怎样？** 运行不报错，`tool_search` 返回"已加载，现在可以调用了"——而模型永远看不到它们。
整个机制静默失效，每一层都说一切正常。所以有一个以这个错误命名的测试。

```python
def _tool(index: int) -> RemoteTool:
    return RemoteTool(
        "bigcorp",
        f"operation_{index:02d}",
        f"Run operation {index} against the configured backend and report on it.",
        {
            "type": "object",
            "required": ["target"],
            "properties": {
                "target": {"type": "string", "description": "What to operate on."},
                "dry_run": {"type": "boolean", "description": "Do not actually do it."},
            },
        },
    )


def test_F09_03_schemas_that_fit_the_budget_are_all_shown() -> None:
    registry = McpRegistry(schema_budget=4000)
    registry.add(None, [_tool(i) for i in range(5)])  # type: ignore[arg-type]
    assert registry.deferred == set()
    assert len(registry.visible) == 5
    assert all(schema["function"]["name"] != "tool_search" for schema in registry.visible)


def test_F09_03_schemas_that_do_not_fit_are_replaced_by_an_index() -> None:
    registry = McpRegistry(schema_budget=4000)
    registry.add(None, [_tool(i) for i in range(60)])  # type: ignore[arg-type]

    assert len(registry.deferred) == 60
    assert len(registry.visible) == 1
    search = registry.visible[0]
    assert search["function"]["name"] == "tool_search"

    full = estimate_messages((), [registry.schema_for(n) for n in sorted(registry.registrations)])
    deferred = estimate_messages((), registry.visible)
    assert deferred < full
    # The index is the cheap half of two-stage loading and the useful half:
    # without the names in the description, the model has to guess a query.
    for name in sorted(registry.deferred)[:3]:
        assert name in search["function"]["description"]


def test_F09_03_the_budget_counts_this_projects_own_tools_too() -> None:
    local = [
        {
            "type": "function",
            "function": {"name": "read_file", "description": "x" * 8000, "parameters": {}},
        }
    ]
    registry = McpRegistry(schema_budget=1000, local=local)
    registry.add(None, [_tool(0)])  # type: ignore[arg-type]
    assert registry.deferred == {"mcp__bigcorp__operation_00"}
    assert [schema["function"]["name"] for schema in registry.visible] == [
        "read_file",
        "tool_search",
    ]


async def test_F09_03_tool_search_reveals_into_the_same_list_the_model_client_holds() -> None:
    """The mistake this is named after: handing over a copy.

    `llm.tools = list(registry.visible)` type-checks, runs, and quietly makes
    `tool_search` do nothing at all -- the tool is revealed into a list the
    request builder is not reading.
    """
    registry = McpRegistry(schema_budget=200)
    registry.add(None, [_tool(i) for i in range(60)])  # type: ignore[arg-type]
    what_the_client_holds = registry.visible  # by reference, exactly as __main__ does

    output = await registry.search_handler()({"query": "operation 07"})
    assert "mcp__bigcorp__operation_07" in output
    assert "mcp__bigcorp__operation_07" in [
        schema["function"]["name"] for schema in what_the_client_holds
    ]
    assert "mcp__bigcorp__operation_07" not in registry.deferred


async def test_F09_03_a_search_that_matches_nothing_says_what_to_do_next() -> None:
    registry = McpRegistry(schema_budget=200)
    registry.add(None, [_tool(i) for i in range(60)])  # type: ignore[arg-type]
    output = await registry.search_handler()({"query": "photosynthesis"})
    assert "No tool matched" in output
    assert "search again" in output.lower()
    assert len(registry.deferred) == 60


async def test_F09_03_a_search_result_says_that_other_tools_remain() -> None:
    """Measured: without this sentence the model settles for a near-miss.

    Six runs out of six, given only the tools its query matched, gpt-4o-mini
    picked an approximately-right tool or asked the user to narrow the task
    down -- never searching a second time.  The sentence is a prompt, and
    chapter 3 (F03-07) is the rule it follows: say what to do next.
    """
    registry = McpRegistry(schema_budget=200)
    registry.add(None, [_tool(i) for i in range(60)])  # type: ignore[arg-type]
    output = await registry.search_handler()({"query": "operation 07"})
    assert "still unloaded" in output
    assert "tool_search again" in output
```

> `_tool(i)` 造一个假工具。五个工具装得下，全部展示，没有 `tool_search`；六十个装不下，只剩一个 `tool_search`，它的说明里有被藏起来的工具的名字；
> 预算把我们自己的工具也算进去；搜索加载的工具出现在**模型客户端拿着的那个列表**里；什么都没搜到时说下一步怎么办；搜到时说"还有别的没加载"。

```bash
git add src/minicodex/registry.py tests/test_faults_ch09.py probe_mcp.py
git commit -m "feat(registry): show every schema until they do not fit, then an index and a search"
```

---

## §8 处理函数：模型调用一个远程工具时发生什么

```python
    def handlers(self) -> dict[str, ToolFn]:
        """A handler per registered tool, deferred ones included.

        Deferring is about the *schema list*, not about what may be called: a
        tool the model learned of through `tool_search` has to be callable on
        the very next turn, and a tool it remembers from before a refresh has
        to reach a real error rather than chapter 0's "no tool named" message,
        which was written for names the model invented.
        """
        return {name: self._handler(name) for name in self.registrations}

    def _handler(self, name: str) -> ToolFn:
        async def handler(arguments: dict[str, Any]) -> str:
            registration = self.registrations.get(name)
            if registration is None:  # forgotten between rendering and calling
                return tool_error(
                    f"{name} is no longer available",
                    you_sent=json.dumps(arguments)[:200],
                    do_this=self.explain(name),
                )
            stopped = registration.client.failure
            if not registration.client.alive:
                # One restart, here, rather than a background supervisor: a
                # server is only worth restarting when something wants to use
                # it, and doing it on the call keeps the retry inside the
                # turn budget the model can see.  `reconnect` may return a
                # different tool set, which is why the registration is looked
                # up again below instead of being reused.
                report = await self.reconnect(registration.tool.server)
                registration = self.registrations.get(name)  # type: ignore[assignment]
                if registration is None:
                    return tool_error(
                        f"{name} could not be called",
                        you_sent=json.dumps(arguments)[:200],
                        do_this=(
                            f"The server providing it stopped ({stopped or 'no reason reported'})"
                            f" and {report}. Use a local tool instead, or tell the user."
                        ),
                    )
            try:
                result = await registration.client.call(registration.tool.name, arguments)
            except (TimeoutError, McpError) as exc:
                # Not retried, on purpose.  The call above was in flight when
                # the server stopped answering, so whether its side effect
                # happened is unknown -- and "unknown" is not a state to
                # resolve by doing it again.  The *next* call finds a dead
                # client and restarts it, which is a retry of something that
                # provably never started.
                return tool_error(
                    f"{name} did not return",
                    you_sent=json.dumps(arguments)[:200],
                    do_this=f"The server said: {exc}. Try a different approach.",
                )
            return normalise(result)

        return handler

    def explain(self, name: str) -> str:
        """What to tell the model about a name that is not registered."""
        if name in self.retired:
            return f"{self.retired[name]}. Use one of the tools you were given instead."
        return "Use one of the tools you were given instead."
```

> - **`handlers()`**：每个登记过的工具一个处理函数——**被藏起来的也有**。藏的是说明，不是"能不能调用"。
> - **`_handler(name)`** 返回的那个函数，按顺序做四件事：
>   1. 这个名字现在还登记着吗？不在了：返回一条说明原因的错误（§12）。
>   2. 它的 server 还活着吗？死了：**重启一次**（§10.3），重启后再查一遍登记。
>   3. 调用——注意发给 server 的是 `registration.tool.name`，**原名**。
>   4. 连接层面失败（超时、断开）：返回三段式的错误，**不重试**（§10.3）。成功：把结果整理成一个字符串（§9）。
> - **`explain(name)`**：一个不在了的名字，该怎么跟模型说。

---

## §9 F09-07：结果的形状五花八门

第 0 章定的规矩是：一个工具返回一个字符串。MCP 的返回是一串"块"，外加两个可选的东西：一份"结构化内容"，一个"出错了"的标记。

```python
def _field(block: Any, *names: str, default: Any = None) -> Any:
    """One block's field, whichever spelling this object uses.

    The SDK hands back typed objects with snake_case attributes
    (`is_error`, `input_schema`) for fields the wire spells in camelCase
    (`isError`, `inputSchema`).  Tests and `load_config` still deal in plain
    dicts, and a server may send a shape the SDK models as an "unknown"
    block.  Rather than convert everything to one representation at the door
    -- which would mean choosing which of the two is real -- this reads
    whichever is there.

    Not cleverness for its own sake: F09-07's whole point is that this
    function must survive shapes it did not expect, and an attribute lookup
    that raises on a dict is exactly the brittleness that fault is about.
    """
    for name in names:
        if isinstance(block, dict):
            if name in block:
                return block[name]
        elif hasattr(block, name):
            value = getattr(block, name)
            if value is not None:
                return value
    return default


def _echoes(structured: Any, parts: list[str]) -> bool:
    """Does `structuredContent` only repeat what the text blocks already said?

    F09-12, and it arrived with the SDK rather than from any server.  MCP says
    a server sending `structuredContent` SHOULD also send the same data as a
    text block, so older clients still see an answer -- and the SDK obliges
    automatically for any tool with a typed return.  A tool that returns
    `"no notes match"` therefore comes back as *both* that text and
    `{"result": "no notes match"}`, and rendering both gives:

        no notes match
        structuredContent: {"result": "no notes match"}

    Twice the tokens, on every call to every remote tool, saying one thing.
    Nothing raised; the tests that caught it were asserting on the answer.

    The rule is deliberately narrow: drop the structured line only when it is
    a single-key wrapper whose value the text already carries verbatim.  A
    genuine structured payload -- several fields, or fields the text does not
    mention -- is exactly the case `structuredContent` exists for, and still
    goes through.  Guessing more aggressively would throw away the machine-
    readable half of a result to save a few tokens.
    """
    if not isinstance(structured, dict) or len(structured) != 1:
        return False
    (value,) = structured.values()
    if not isinstance(value, str | int | float | bool) or isinstance(value, bool):
        return False
    return str(value) in parts


def normalise(result: Any) -> str:
    """Turn one MCP result into the single string chapter 0 promised.

    MCP results are a list of typed blocks plus two optional extras, and
    servers disagree about which of them carries the answer.  Every shape has
    to survive this function, including the ones that carry no text at all:
    a result rendered as an empty string is indistinguishable from a tool that
    succeeded and had nothing to say, and the model treats that as an answer.

    Binary payloads are described, not included.  A base64 PNG is tokens the
    model cannot read (unless it is multi-modal, which is F06-10's unfinished
    business) and it would land in the history at full length.

    Takes `Any` rather than `CallToolResult`: the client returns the SDK's
    typed result, but this function is also handed plain
    dicts by tests that need to express a shape no well-behaved server would
    produce.  Accepting both is what lets F09-07's table stay a table.
    """
    blocks = _field(result, "content", default=None)
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict) and not hasattr(block, "type"):
                parts.append(f"[non-object content block: {type(block).__name__}]")
                continue
            kind = _field(block, "type")
            if kind == "text":
                parts.append(str(_field(block, "text", default="")))
            elif kind == "image":
                mime = _field(block, "mimeType", "mime_type", default="image/?")
                size = len(str(_field(block, "data", default="")))
                parts.append(f"[image omitted: {mime}, {size} base64 characters]")
            elif kind == "audio":
                mime = _field(block, "mimeType", "mime_type", default="audio/?")
                parts.append(f"[audio omitted: {mime}]")
            elif kind == "resource":
                resource = _field(block, "resource", default=None) or {}
                uri = _field(resource, "uri", default="?")
                body = _field(resource, "text", default=None)
                if isinstance(body, str):
                    parts.append(f"[resource {uri}]\n{body}")
                else:
                    parts.append(f"[binary resource omitted: {uri}]")
            elif kind == "resource_link":
                parts.append(f"[resource link: {_field(block, 'uri', default='?')}]")
            else:
                parts.append(f"[unsupported content block of type {kind!r}]")

    structured = _field(result, "structuredContent", "structured_content", default=None)
    if structured is not None:
        if hasattr(structured, "model_dump"):
            structured = structured.model_dump(mode="json")
        if not _echoes(structured, parts):
            parts.append("structuredContent: " + json.dumps(structured, ensure_ascii=False))

    text = "\n".join(part for part in parts if part) or "(the tool returned no content)"

    if _field(result, "isError", "is_error", default=False):
        # The call succeeded at the protocol level and failed at the tool
        # level.  Two different failures, one of which chapter 0 already has a
        # rule for: it is text the model acts on.  Saying so is the difference
        # between the model retrying and the model believing it.
        text = f"The tool reported an error.\n{text}"

    if len(text) > MAX_RESULT_CHARS:
        half = MAX_RESULT_CHARS // 2
        omitted = len(text) - 2 * half
        text = f"{text[:half]}\n... ({omitted} characters omitted) ...\n{text[-half:]}"
    return text
```

> - **`_field(block, *names)`**：取一个字段，不管这个东西是字典还是 SDK 给的对象，也不管名字是 `isError` 还是 `is_error`——SDK 在 Python 这边用下划线的名字，
>   测试里手写的字典用协议原来的名字。
> - **`normalise(result)`**：逐块处理——
>   - 文字：原样。
>   - **图片、音频：描述它，不放进去。** 一张图片的编码对读不了图片的模型是纯噪音，而且它会原样进历史，往后每一轮都重发。
>     变成 `[image omitted: image/png, 96 base64 characters]`。
>   - 内嵌的资源：有文字就带上地址和文字；没有就说"省略了"。不认识的块：**说出来**，不悄悄跳过。
>   - "结构化内容"：附在后面——除非它只是把文字重复了一遍（`_echoes`，见下）。
>   - **什么都没有：必须说自己是空的**——`(the tool returned no content)`。空字符串在模型看来和"成功了，没什么可说的"一模一样。
>   - **"出错了"的标记要说出来**：`The tool reported an error.`。这是"工具运行了但失败了"——连接本身是好的。不加这一句，一条失败的消息和一条正常的返回在模型眼里没有区别。
>   - 太长：头尾各留一半，中间说明省略了多少。第 2 章截的是 shell 的输出，第 6 章截的是历史里的条目，**远程工具的结果两个都不经过**。
> - **`_echoes(structured, parts)`**：SDK 会自动把"带类型的返回值"再包成一份结构化内容。于是一个返回 `"no notes match"` 的工具，回来的既有这句文字，
>   又有 `{'result': 'no notes match'}`——两份都渲染，每次调用都多花一倍 token 说同一件事。规则故意很窄：只有"单个键、值已经在文字里原样出现过"才丢掉。

对真的 server 调用几次（实测）：

```
stat nope -> 'The tool reported an error.\nError executing tool stat'
render    -> 'note \'idea\' rendered\n[image omitted: image/png, 96 base64 characters]\n[resource notes://idea]\nA tool nobody can find is a tool nobody calls.\nstructuredContent: {"note": "idea", "bytes": 46}'
search    -> 'no notes match'
raw structured_content of that search: {'result': 'no notes match'}
```

```python
def test_F09_07_text_blocks_are_joined() -> None:
    assert normalise(
        {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    ) == ("a\nb")


def test_F09_07_binary_blocks_are_described_not_included() -> None:
    out = normalise({"content": [{"type": "image", "data": "A" * 4000, "mimeType": "image/png"}]})
    assert out == "[image omitted: image/png, 4000 base64 characters]"
    assert "AAA" not in out


def test_F09_07_an_embedded_resource_keeps_its_text_and_its_uri() -> None:
    out = normalise(
        {
            "content": [
                {
                    "type": "resource",
                    "resource": {"uri": "notes://idea", "text": "the note body"},
                }
            ]
        }
    )
    assert "notes://idea" in out
    assert "the note body" in out


def test_F09_07_structured_content_is_not_dropped() -> None:
    out = normalise({"content": [], "structuredContent": {"bytes": 46}})
    assert '"bytes": 46' in out


def test_F09_07_an_empty_result_says_so_rather_than_being_an_empty_string() -> None:
    """An empty string reads as "the tool succeeded and had nothing to say"."""
    assert normalise({}) == "(the tool returned no content)"
    assert normalise({"content": []}) == "(the tool returned no content)"


def test_F09_07_is_error_is_stated_because_the_transport_succeeded() -> None:
    out = normalise({"content": [{"type": "text", "text": "no such file"}], "isError": True})
    assert out.startswith("The tool reported an error.")
    assert "no such file" in out


def test_F09_07_an_unknown_block_type_is_reported_not_skipped() -> None:
    out = normalise({"content": [{"type": "hologram", "data": "..."}]})
    assert "hologram" in out


def test_F09_07_a_huge_result_is_clipped_at_both_ends() -> None:
    """The MCP path reaches the history through neither chapter 2's shell clip
    nor chapter 6's per-item clip; it needs its own."""
    body = "START" + "x" * 500_000 + "END"
    out = normalise({"content": [{"type": "text", "text": body}]})
    assert len(out) < MAX_RESULT_CHARS + 200
    assert out.startswith("START")
    assert out.endswith("END")
    assert "characters omitted" in out


async def test_F09_07_a_real_server_returning_three_block_types_survives() -> None:
    registry, clients = await started(notes())
    try:
        out = await registry.handlers()["mcp__notes__render"]({"name": "idea"})
        assert "rendered" in out
        assert "[image omitted: image/png" in out
        assert "notes://idea" in out
        assert '"bytes": 46' in out
    finally:
        await stop(clients)
```

> 前八个直接喂字典给 `normalise`，一种形状一个；最后一个走真的子进程、真的协议、真的图片。**F09-07：动工前就挡住了。**

---

## §10 F09-04、F09-05：server 起不来，或者中途死了

### 10.1 起不来的

```python
async def connect(
    configs: list[ServerConfig],
    registry: McpRegistry,
    *,
    handlers: dict[str, Any] | None = None,
) -> list[McpClient]:
    """Start every configured server, and survive the ones that do not start.

    A server that fails is recorded and skipped.  It is not raised: an agent
    that refuses to run because one of five optional servers is broken is
    worse at its job than one that runs with four (F09-04).
    """
    clients: list[McpClient] = []
    for config in configs:
        client = McpClient(config, handlers=handlers)
        try:
            await client.start()
            registry.add(client, await client.list_tools())
        except (McpError, TimeoutError, OSError) as exc:
            registry.failures[config.name] = str(exc)
            await client.close()
            continue
        clients.append(client)
    return clients
```

> 一个个启动。**启动失败的记下来、跳过，不抛异常**：配了五个 server、其中一个坏了，"用剩下四个跑"比"整个 Agent 拒绝启动"好得多。

实测，三个 server：一个故意启动要 30 秒（启动期限设成 1 秒），一个命令根本不存在，一个正常：

```
clients: ['notes']
failure[slow]: slow did not answer initialize within 1s
failure[ghost]: could not start ghost: [WinError 2] 系统找不到指定的文件。
```

```python
async def test_F09_04_a_slow_server_does_not_hold_the_agent_hostage() -> None:
    registry = McpRegistry()
    slow = ServerConfig(
        name="slow",
        command=(sys.executable, str(SERVERS / "files_server.py")),
        env={"MCP_STARTUP_DELAY": "30"},
        startup_timeout=1.0,
    )
    started_at = asyncio.get_running_loop().time()
    clients = await connect([slow], registry)
    elapsed = asyncio.get_running_loop().time() - started_at

    assert clients == []
    assert registry.registrations == {}
    assert "did not answer initialize within 1s" in registry.failures["slow"]
    assert elapsed < 10  # the timeout, not the server's 30 seconds


async def test_F09_04_one_broken_server_does_not_take_the_working_ones_with_it() -> None:
    """An agent that refuses to run because one optional server is down is
    worse at its job than one that runs with the rest."""
    registry, clients = await started(
        ServerConfig("ghost", ("definitely-not-a-program-xyz",)),
        notes(),
    )
    try:
        assert "ghost" in registry.failures
        assert [c.config.name for c in clients] == ["notes"]
        assert "mcp__notes__search" in registry.registrations
    finally:
        await stop(clients)
```

### 10.2 中途死的：它得能说出原因

让 `files` 在第 1 次调用之后死掉。它死之后，SDK 只能告诉我们一句 `Connection closed`。**这句话是真的，也是没用的**——为什么关的？

server 的遗言在它的 stderr 上。SDK 默认把子进程的 stderr 接到我们自己的终端——遗言印在屏幕上，而客户端手里什么都没有，更没法转告模型。
好在 SDK 允许换掉这个默认值：`stdio_client(params, errlog=文件)`。这就是 `mcp.py` 里 `_target` 那段临时文件的用途，`_why` 在连接出问题时去读它的最后一行。

> **判断一个依赖好不好用的一个实际标准：它有没有把自己的默认值，做成一个你能换掉的参数。**

实测两种死法：

```
悄悄退出（MCP_DIE_AFTER=1）
call 0: 3768
call 1: Error: mcp__files__stat did not return You sent: {"name": "pyproject.toml"} The server said: files: Connection closed. Try a different approach.
call 2: 3768

死之前往 stderr 写了一行（MCP_CRASH_AFTER=1）
Error: mcp__files__stat did not return You sent: {"name": "pyproject.toml"} The server said: files: Connection closed: MemoryError: index too large to load. Try a different approach.
```

第二种，模型拿到了原因。**第一种，只有"连接关了"**：一个干干净净退出、什么都不说的 server，没有任何办法知道它为什么死——连退出码都拿不到，因为子进程在 SDK 手里，不借给我们。
这是用 SDK 的一个代价，**没有绕过去，而是写成了测试**，测试的名字就叫"悄悄的死，SDK 只能告诉我们这么多"。

> 把缺口写成测试，比写进注释可靠：注释不会在有人以为自己把它修好了的时候变红。

### 10.3 重启，和一个故意不做的重试

上面第一种死法的三次调用里，第 2 次（`call 2`）成功了——处理函数发现 server 死了，就地重启了一次。重启放在**调用的时候**做，不放在后台：
一个 server 只有在有人要用它时才值得重启，而且这样重启花的时间落在模型看得见的那一轮里。

第 1 次调用（发现它死了的那次）**没有被重试**，这是故意的：那次调用发出去的时候 server 还活着，它到底执行了没有、执行了一半没有，这边不知道。
**"不知道"不是一个靠"再做一次"来消除的状态。** 下一次调用面对的是一个确定已经死了的 server，重启后再做，重试的是一件确定没开始过的事。

```python
    async def reconnect(self, server: str) -> str:
        """Restart one server and re-read its tool list.

        Re-read, not restore: a server that has been restarted may come back
        with a different set of tools, and pretending otherwise leaves
        registrations pointing at names the new process does not answer to.
        The tool list is the server's to declare, every time it starts.
        """
        registrations = [r for r in self.registrations.values() if r.tool.server == server]
        client = registrations[0].client if registrations else None
        if client is None:
            return f"{server} is not a server this session knows about"
        await client.close()
        client.failure = None
        try:
            await client.start()
            tools = await client.list_tools()
        except (McpError, TimeoutError, OSError) as exc:
            self.failures[server] = str(exc)
            self.forget(server, f"the MCP server {server!r} stopped and would not restart: {exc}")
            return f"{server} could not be restarted: {exc}"
        before = {r.model_name for r in registrations}
        was_deferring = any(s["function"]["name"] == "tool_search" for s in self.visible)
        self.forget(server, f"the MCP server {server!r} restarted without this tool")
        # Only the names this session already had come back.  The loop's
        # handler table was built once, when the session started, and nothing
        # rebuilds it -- so a tool that first appears now would be *shown* to
        # the model and answer "no tool named ..." when called.  Measured:
        # exactly that happened, which is the lie `forget()` exists to avoid,
        # told about a tool that is really there.  A restart may take tools
        # away; it does not get to add any until the next session.
        returning = [tool for tool in tools if model_name(tool.server, tool.name) in before]
        withheld = sorted(
            name for tool in tools if (name := model_name(tool.server, tool.name)) not in before
        )
        self.add(client, returning)
        # The same reason, one more time: whether there is a `tool_search` is
        # decided at startup, because that is when its handler is (or is not)
        # put in the table.  A restart that changes how much the schemas cost
        # must not flip that under a running session.
        self._restage(defer=was_deferring)
        after = {name for name, r in self.registrations.items() if r.tool.server == server}
        # Deliberately not silent.  A tool set that changed under the model is
        # the thing that makes its next call fail, and a log line here is the
        # only place a human ever finds out it happened.
        gone = sorted(before - after)
        changes = []
        if gone:
            changes.append(f"gone: {', '.join(gone)}")
        if withheld:
            changes.append(f"new, not offered until the next session: {', '.join(withheld)}")
        return f"{server} restarted ({'; '.join(changes) or 'same tools'})"
```

> - 找到这个 server 的客户端，关掉，重新启动，**重新读一遍工具列表**——重启后的 server 可能带着不一样的工具回来，按原来的恢复就是在假装。
> - 起不来：把它的工具全部标成"不在了"（§12），返回原因。
> - 起来了：中间那两段带注释的代码，是 §11.2 的故事。最后返回一句报告，说哪些工具没了、哪些是新的。

```python
async def test_F09_05_a_silent_death_is_all_the_sdk_can_tell_us() -> None:
    """What the SDK cost, asserted rather than glossed (F09-15).

    This test used to assert `"exited with code 1"`, built from
    `proc.returncode` by a client that owned the subprocess.  The SDK owns it
    now and does not lend it out: `stdio_client` yields streams, and the
    process handle stays inside.  So for a server that exits cleanly and says
    nothing on the way out, the honest report really is only that the
    connection closed.

    Kept as a test, and named for the loss, because the alternative is a gap
    nobody sees until they need it.  The informative half of F09-05 still
    works and is the test below: a server that *crashes* leaves a traceback on
    stderr, and that is the case where "why" exists at all.

    The trade this chapter made -- HTTP, OAuth, resources and prompts for an
    exit code -- is worth it, and it is not free, and a book that only
    reported the first half would be selling something.
    """
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__stat"]
        assert (await handler({"name": "pyproject.toml"})).isdigit()
        second = await handler({"name": "pyproject.toml"})
        # The connection is reported as gone...
        assert "Connection closed" in second
        # ...and this is the part that is no longer knowable.
        assert "exited with code" not in second
    finally:
        await stop(clients)


async def test_F09_05_the_reason_comes_from_the_servers_stderr_not_just_its_exit_code() -> None:
    """The mutation that survived the first mutation run.

    `test_F09_05_a_dead_server_reports_why...` looked like it covered this and
    did not: "exited with code 1" is built from `returncode`, so replacing
    `stderr=PIPE` with `stderr=DEVNULL` left every test in this file green.
    A server that crashes rather than exiting cleanly is what tells them
    apart -- its explanation exists only on the pipe.
    """
    registry, clients = await started(files(MCP_CRASH_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__stat"]
        assert (await handler({"name": "pyproject.toml"})).isdigit()
        second = await handler({"name": "pyproject.toml"})
        assert "MemoryError: index too large to load" in second
    finally:
        await stop(clients)


async def test_F09_05_the_call_after_the_death_reconnects_and_succeeds() -> None:
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__stat"]
        assert (await handler({"name": "pyproject.toml"})).isdigit()
        await handler({"name": "pyproject.toml"})  # the one that finds it dead
        third = await handler({"name": "pyproject.toml"})
        assert third.isdigit()
    finally:
        await stop(clients)


async def test_F09_05_the_in_flight_call_is_not_retried() -> None:
    """Deliberate: whether its side effect happened is unknown.

    A retry here would be the idempotency fault (F12-04) chosen on purpose.
    The *next* call is retried instead, because that one provably never ran.
    """
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__search"]
        await handler({"query": "hatchling"})
        failed = await handler({"query": "hatchling"})
        assert "did not return" in failed
        assert "Try a different approach" in failed
    finally:
        await stop(clients)


async def test_F09_05_a_restart_re_reads_the_tool_list_rather_than_restoring_it() -> None:
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__stat"]
        await handler({"name": "pyproject.toml"})
        await handler({"name": "pyproject.toml"})
        report = await registry.reconnect("files")
        assert "restarted" in report
        assert "mcp__files__stat" in registry.registrations
    finally:
        await stop(clients)
```

> 悄悄的死只有 `Connection closed`，而且**没有**退出码；带遗言的死，遗言到了模型那里；死后的下一次调用成功了；发现它死了的那次没有重试；重启会重新读工具列表。
> 第二个测试的 docstring 记着它的来历：变异测试把 stderr 丢掉，当时全部测试都是绿的——原来那个名字里写着"报告原因"的测试，断言的是另一样东西。

---

## §11 两个意外，都出在重启上

### 11.1 两个 server 时，重启其中一个，Agent 被"打断"了

§10 的每个重启测试都只连了**一个** server。这一章自带的示例配置连的是**两个**。改写时把两个都连上，让其中一个死掉，再调用它：

```
--- two servers, one dies and is restarted
  files call 0: '3768'
  files call 1: 'Error: mcp__files__stat did not return You sent: {"name": "pyproject.t'
  !! CancelledError: Cancelled via cancel scope 22bba982fd0 by <Task pending name='Task-1' ...
  !! next await after closing: CancelledError: Cancelled via cancel scope 22bba982fd0 by <Task pending ...
```

第三次调用——触发重启的那一次——没有返回结果，而是抛出了 `CancelledError`。而且从那以后，这个任务里的**每一个** `await` 都被取消。

`CancelledError` 是第 7 章专门处理过的东西：它的意思是"用户按了 Ctrl-C"。所以在真的 Agent 里，这次重启会被报告成**"被用户打断了"**——
一个 server 死了，Agent 却告诉模型和用户，是用户喊的停。

原因在 SDK 的底层。它的连接用了一种叫"取消范围"的东西，有两条规矩：**在哪个任务里进入，就得在哪个任务里离开；后进入的要先离开。**
当时的写法是：会话的那一个任务，先后进入了 `files` 的连接和 `notes` 的连接。重启 `files`，就是在 `notes` 的还开着的时候，去离开**先**进入的那一个——顺序反了。
只连一个 server 时没有"顺序"可言，所以永远测不出来。

修法不是去小心地安排顺序，而是**让顺序这个问题不存在**：每条连接由它自己的一个任务来进入和离开——§4 里 `mcp.py` 的 `_hold`。
一个范围在这个任务里进入、在这个任务里离开，别的连接在做什么和它无关。

它顺带修好了另一件没人测过的事：在 Python 3.10 和 3.11 上，`asyncio.wait_for` 会把交给它的东西放到一个临时的任务里去跑——
原来那种"在 `wait_for` 里面进入连接"的写法，在那两个版本上等于"在一个任务里进入、在另一个任务里离开"。

修完之后，同样的操作：

```
--- two servers, one dies and is restarted; closed in the order they were opened
  files call 0: '3768'
  files call 1: 'Error: mcp__files__stat did not return You sent: {"name": "pyproject.t'
  files call 2: '3768'
  notes still works: idea: A tool nobody can find is a tool n
  next await after closing: fine
```

```python
async def test_F09_17_restarting_one_of_two_servers_leaves_the_caller_alone() -> None:
    """Found with two servers connected, and invisible with one.

    The SDK's connection is built on anyio, whose cancel scopes belong to the
    task that entered them and have to be left in reverse order.  The first
    version entered every server's connection from the session's own task --
    so restarting `files` while `notes` was still open left the outer scope
    first.  The call that triggered the restart raised
    `CancelledError: Cancelled via cancel scope ...` in the *agent's* task,
    which chapter 7 reports as "interrupted by the user", and every await
    after it in that task was cancelled as well.

    Every reconnect test above uses one server, which is why none of them saw
    it.  The example config this chapter ships has two.
    """
    registry, clients = await started(files(MCP_DIE_AFTER="1"), notes())
    try:
        handlers = registry.handlers()
        stat = handlers["mcp__files__stat"]
        assert (await stat({"name": "pyproject.toml"})).isdigit()
        assert "did not return" in await stat({"name": "pyproject.toml"})  # files is gone
        # The call that restarts `files` while `notes` is still connected.
        assert (await stat({"name": "pyproject.toml"})).isdigit()
        found = await handlers["mcp__notes__search"]({"query": "idea"})
        assert "A tool nobody can find" in found
    finally:
        await stop(clients)
    # ...and the task that did all of that was not cancelled behind its back.
    await asyncio.sleep(0)
```

> **测试用的配置比真实用的配置简单，简单掉的那一部分就是没被测到的那一部分。** 每个重启测试都很合理地"只需要一个 server"。

### 11.2 重启后多出来的工具：看得见，调不了

`reconnect` 会重新读工具列表。如果重启后的 server 比原来**多**了一个工具呢？实测（修之前）：

```
tools at startup:       ['mcp__files__search', 'mcp__files__stat']
schemas the model sees: ['mcp__files__filler_00', 'mcp__files__search', 'mcp__files__stat']
handlers the loop has:  ['mcp__files__search', 'mcp__files__stat']
calling the new tool:   Error: no tool named 'mcp__files__filler_00'. Available tools: mcp__files__search, mcp__files__stat. Call one
```

新工具出现在了给模型看的列表里（那个列表是"活的"，§7.5）；但循环手里的处理函数表是会话开始时建的，**没有任何东西会去更新它**。
于是模型调用一个它刚刚被告知存在的工具，得到的是第 0 章那句"没有这个工具"——那句话是为模型**自己编出来**的名字写的。

这一章在 §12 专门写了一套机制，就是为了不对模型说这句假话；而在重启这条路上，它对一个**真实存在**的工具说了。

修法选了简单的那个：**重启可以让工具变少，不能让工具变多。** `reconnect` 里只把原来就有的名字登记回来；新出现的不给模型看，写进返回的报告里
（"new, not offered until the next session"）。同一个道理还有另一半：`tool_search` 有没有，也是在会话开始时定的（它的处理函数那时候才会被放进表里），
所以重启不能让展示方式从"全部展示"翻成"藏起来"——`self._restage(defer=was_deferring)`。

对给模型看的工具列表，这条性质必须永远成立：**列表里的每一个，循环都调得了。**

```python
async def test_F09_05_a_tool_that_first_appears_after_a_restart_is_withheld() -> None:
    """A restarted server may come back offering *more* than it did.

    The first version re-registered whatever the new process declared.  The
    schema list is live, so the new tool was shown to the model at once -- and
    the loop's handler table is not, so calling it answered

        Error: no tool named 'mcp__files__filler_00'. Available tools: ...

    which is chapter 0's message for a name the model made up, said about a
    tool the model had just been handed.  Found by restarting a server with
    one more tool and asking for it.  The property every tool list shown to
    the model has to have is plain: nothing in it that the loop cannot call.
    """
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        callable_names = set(registry.handlers())  # what the loop is given, once
        handler = registry.handlers()["mcp__files__stat"]
        await handler({"name": "pyproject.toml"})
        await handler({"name": "pyproject.toml"})  # the call that finds it dead

        # The restart comes back with one more tool than before.
        clients[0].config = dataclasses.replace(clients[0].config, env={"MCP_EXTRA_TOOLS": "1"})
        report = await registry.reconnect("files")

        shown = {schema["function"]["name"] for schema in registry.visible}
        assert shown <= callable_names, f"shown but not callable: {sorted(shown - callable_names)}"
        assert "mcp__files__filler_00" in report
        assert "not offered until the next session" in report
    finally:
        await stop(clients)


async def test_F09_05_a_restart_does_not_change_whether_there_is_a_tool_search() -> None:
    """`tool_search` gets its handler at startup or not at all.

    So a restart after which the same tools cost more -- a longer description
    is enough -- must not start deferring them: the model would be shown a
    `tool_search` that nothing in the loop answers to.
    """

    class Restartable:
        failure: str | None = None
        alive = False

        def __init__(self) -> None:
            self.description = "short"

        async def close(self) -> None:
            pass

        async def start(self) -> None:
            pass

        async def list_tools(self) -> list[RemoteTool]:
            return [RemoteTool("s", "t", self.description, {"type": "object"})]

    client = Restartable()
    registry = McpRegistry(schema_budget=200)
    registry.add(client, await client.list_tools())  # type: ignore[arg-type]
    assert registry.deferred == set()

    client.description = "x" * 5000  # the same tool, now far over the budget
    await registry.reconnect("s")

    assert [schema["function"]["name"] for schema in registry.visible] == ["mcp__s__t"]
    assert registry.deferred == set()
```

> - 第一个：重启后带着一个新工具回来；给模型看的名字是"循环能调用的名字"的子集，报告里说了新工具暂不提供。
>   `dataclasses.replace(config, env=...)`：复制一份配置，只改环境变量。
> - 第二个：用一个假的客户端（`Restartable`），重启后同一个工具的说明变得很长、超过了预算——仍然不出现 `tool_search`。

```bash
git add src/minicodex/mcp.py src/minicodex/registry.py tests/test_faults_ch09.py
git commit -m "feat(registry): normalise results, survive servers that fail, and restart them without surprises"
```

---

## §12 F09-08：历史里有这个工具，现在没有了

模型在十轮之前用过 `mcp__notes__search`，它在上下文里看得清清楚楚；现在那个 server 断开了。模型再调用一次，会怎样？

第 0 章那句"没有这个工具"在这里是一句假话，而且模型对它的反应是**更使劲地试**。所以注册表在去掉一个工具时，**记住它曾经在**：

```python
    def forget(self, server: str, reason: str | None = None) -> None:
        """Drop a server's tools, remembering that they were once there.

        Remembering matters more than dropping.  The model has the old names
        in its context and will use them; chapter 0's "no tool named X" was
        written for names a model *invented*, and telling it that about a tool
        it genuinely had ten seconds ago is a lie that makes it try harder.
        """
        because = reason or f"the MCP server {server!r} is no longer connected"
        for name, registration in list(self.registrations.items()):
            if registration.tool.server == server:
                self.retired[name] = because
                del self.registrations[name]
        self._restage()
```

> 把这个 server 的工具从 `registrations` 里拿掉，同时在 `retired` 里记下原因。**记住比拿掉更重要。**

处理函数还在循环的表里（`_handler` 第一步），于是模型得到的是：

```
Error: mcp__notes__search is no longer available You sent: {"query": "idea"} the MCP server 'notes' is no longer connected. Use one of the tools you were given instead.
```

```python
async def test_F09_08_a_retired_tool_says_what_happened_to_it() -> None:
    """Chapter 0's "no tool named X" was written for names the model made up.

    Telling a model that about a tool it genuinely used ten turns ago is a
    lie, and the model responds to it by trying harder.
    """
    registry, clients = await started(notes())
    try:
        handler = registry.handlers()["mcp__notes__search"]
        registry.forget("notes")
        answer = await handler({"query": "idea"})
        assert "no longer available" in answer
        assert "no longer connected" in answer
    finally:
        await stop(clients)


async def test_F09_08_a_server_that_will_not_restart_retires_its_tools() -> None:
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__stat"]
        await handler({"name": "pyproject.toml"})
        await handler({"name": "pyproject.toml"})
        registry.registrations["mcp__files__stat"].client.config = ServerConfig(
            "files", ("definitely-not-a-program-xyz",)
        )
        answer = await handler({"name": "pyproject.toml"})
        assert "could not be called" in answer
        assert "could not be restarted" in answer
        assert registry.registrations == {}
    finally:
        await stop(clients)


def test_F09_08_an_unknown_name_that_was_never_registered_gets_the_plain_advice() -> None:
    registry = McpRegistry()
    assert "Use one of the tools you were given" in registry.explain("mcp__ghost__thing")
```

**F09-08：动工前就挡住了。**

---

## §13 F09-06：server 反过来问你

MCP 里 server 可以向客户端要一个答复：调用执行到一半，需要一个确认。`notes` 的 `remember` 在设了 `MCP_ELICIT` 时就会问"要保存吗？"。**谁来回答？**

第 5 章的那个 `Approver`。不加任何新的审批方式——同一种问题有第二种问法，用户就学会不看内容直接回答了。

```python
def elicitation_handler(session: Session) -> RequestHandler:
    """Answer a server's `elicitation/create` with chapter 5's approver.

    MCP lets a server ask the *client* a question mid-call -- a confirmation,
    a missing field, an OAuth consent.  There is no new approval machinery
    here on purpose: chapter 5 already decided who gets asked and how, and a
    second prompt style for the same question is how a user learns to answer
    without reading.

    Two shapes are refused rather than guessed at.  A server may request a
    whole object of fields, and this client can answer exactly one kind of
    question -- yes or no.  `decline` is a legal MCP answer and means "the
    user said no"; the alternative, making something up to fill the schema,
    would put an invented value into somebody else's system.
    """

    async def handle(params: dict[str, Any]) -> dict[str, Any]:
        message = str(params.get("message") or "a server is asking for confirmation")
        schema = params.get("requestedSchema") or {}
        properties = schema.get("properties") or {}
        booleans = [
            key for key, spec in properties.items() if (spec or {}).get("type") == "boolean"
        ]
        if len(properties) != 1 or not booleans:
            return {"action": "decline"}
        reply = await session.approver.ask(
            ApprovalRequest(
                what=message,
                reason="an MCP server is asking before it acts",
                risk=Risk.UNKNOWN,
                suggested_rule=None,
            )
        )
        if not reply.approved:
            return {"action": "decline"}
        return {"action": "accept", "content": {booleans[0]: True}}

    return handle
```

> - 返回一个函数 `handle(params)`：拿到 server 的问题和它想要的答案的形状。
> - **只回答一种问题：是或否**（恰好一个字段，而且是布尔类型）。别的形状——比如"请给我你的 API key"——一律回答 `decline`（"用户不同意"，协议里合法的回答）。
>   另一种做法是编一个值把它填满：**那是把一个凭空捏造的值写进别人的系统。**
> - 是或否的问题，交给 `session.approver.ask(...)`。

### 13.1 而协议已经不让 server 这么问了

实际去调用那个会提问的 `remember`：

```
McpError: notes: Cannot send 'elicitation/create': this transport context has no back-channel for server-initiated requests.
```

SDK 现在说的协议版本（`2026-07-28`）**不允许 server 主动向客户端发请求**——SDK 的源码注释里直接写着这句话。一个要提问的 server 得到的不是答案，而是这个错误。

所以这一节的机制，在这一章里测到的是它**失败得干不干净**：调用很快失败、说明了原因、要写的东西没有被写下去。挂在那里等一个永远不会来的答复，才是真正糟糕的结果。

`elicitation_handler` 和 `mcp.py` 里把它接到 SDK 上的 `_as_callback` 仍然留着：它做的事（把"一条消息加一个形状"变成"问人一个是非题"）和用哪个协议版本传递无关，
后面讲远程 MCP 的那一章会加一种问题能送达的连接方式。

> 如果客户端是自己手写的、把协议版本固定在两年前，这个变化根本发现不了——它会一直协商一个那个机制还存在的旧版本，一直通过测试，直到某个 server 不肯再说旧版本为止。
> **把传输层冻住，就是把日历冻住。** 这是依赖 SDK 换来的东西之一。

```python
async def test_a_server_request_this_client_does_not_handle_fails_fast_not_forever() -> None:
    """The property that survived the protocol change (see F09-16).

    The original worry was that a server-initiated request nobody answers
    leaves the server waiting forever, and with it the call the model is
    blocked on.  Under protocol `2026-07-28` the request cannot be sent at
    all, so the shape of the failure changed -- but the *property* is the one
    that mattered and it still holds: the attempt ends promptly and says why,
    rather than hanging until a timeout expires.

    `McpError` rather than an error result, because this is a transport-level
    refusal and not a tool that ran and failed -- the distinction chapter 0
    drew and `call()` still keeps.
    """
    client = McpClient(notes(MCP_ELICIT="1"))
    await client.start()
    try:
        with pytest.raises(McpError, match="back-channel"):
            await client.call("remember", {"name": "x", "text": "y"})
    finally:
        await client.close()


async def test_F09_06_the_protocol_removed_the_mechanism_this_fault_was_fixed_with() -> None:
    """The sharpest thing the SDK rewrite found (F09-16).

    F09-06 was written against protocol `2025-06-18`, where a server could
    send the *client* a JSON-RPC request mid-call -- `elicitation/create` --
    and `registry.elicitation_handler` answered it with chapter 5's approver.
    That worked, was measured, and shipped.

    Protocol `2026-07-28` forbids server-initiated requests outright. The
    SDK's own dispatcher says so in as many words
    (`mcp/server/runner.py:551-558`, `_NoServerRequestsDispatchContext`:
    *"the modern protocol forbids server-initiated JSON-RPC requests"*), and a
    server that tries gets `NoBackChannelError` rather than an answer.

    **The hand-rolled client could not have found this.** It pinned
    `PROTOCOL_VERSION = "2025-06-18"`, so it would have kept negotiating a
    version where the mechanism still existed, kept passing this chapter's
    tests, and kept working -- until a server declined to speak a two-year-old
    revision. Freezing the transport froze the calendar.

    What is asserted here is what a client can still guarantee: the call fails
    **cleanly and quickly**, it says why, and the side effect does not happen.
    A hang would have been the bad outcome, and F09-04's deadline is what
    rules it out.
    """
    session = Session(mode="workspace-write", policy="on-request", approver=AllowAll())
    db = _scratch("_notes_approved.json")
    registry, clients = await started(
        notes(MCP_ELICIT="1", MCP_NOTES_DB=str(db)),
        handlers={"elicitation/create": elicitation_handler(session)},
    )
    try:
        result = await registry.handlers()["mcp__notes__remember"]({"name": "x", "text": "hi"})
        assert "back-channel" in result
        # The note was never written: the tool raised before touching disk.
        assert not db.exists()
    finally:
        await stop(clients)
        await asyncio.to_thread(db.unlink, True)


async def test_F09_06_the_client_still_offers_the_capability_it_can_honour() -> None:
    """`elicitation_handler` is kept, and this says why rather than leaving it
    looking like dead code.

    The adapter from an MCP elicitation to chapter 5's approver is unaffected
    by which protocol revision carries the question -- it maps a message and a
    schema onto "ask a human yes or no", and declines anything wider. What
    changed is only the envelope. Chapter 21 adds a transport where the
    question can arrive again; deleting the answer in the meantime would mean
    rediscovering chapter 5's rule about a second prompt style for the same
    question.
    """
    session = Session(mode="workspace-write", policy="on-request", approver=AllowAll())
    handler = elicitation_handler(session)
    reply = await handler(
        {
            "message": "Save note 'x' to disk?",
            "requestedSchema": {
                "type": "object",
                "required": ["confirm"],
                "properties": {"confirm": {"type": "boolean"}},
            },
        }
    )
    assert reply == {"action": "accept", "content": {"confirm": True}}

    denied = elicitation_handler(
        Session(mode="workspace-write", policy="on-request", approver=DenyAll())
    )
    assert (await denied({"message": "?", "requestedSchema": {}}))["action"] == "decline"


async def test_F09_06_a_request_for_a_shape_we_cannot_answer_declines() -> None:
    """Filling in a schema this client cannot ask a human about would mean
    inventing a value and putting it into somebody else's system."""
    asked: list[str] = []

    class Recording:
        async def ask(self, request: Any) -> ApprovalReply:
            asked.append(request.what)
            return ApprovalReply(True, request.what)

    handler = elicitation_handler(
        Session(mode="workspace-write", policy="on-request", approver=Recording())
    )
    answer = await handler(
        {
            "message": "what is your API key?",
            "requestedSchema": {
                "type": "object",
                "properties": {"api_key": {"type": "string"}},
            },
        }
    )
    assert answer == {"action": "decline"}
    assert asked == []
```

> 提问的调用很快以 `McpError` 结束；经过注册表时，模型得到一条错误，笔记文件没有被创建；`elicitation_handler` 自己：是非题会去问 Approver，同意就回答接受、不同意就回答拒绝；
> 要 API key 的问题直接拒绝，**根本没去问人**（`asked == []`）。

---

## §14 第 8 章的脚印，遇上别人的一句话

第 8 章留下一句："没有复现"的那条故障（调度器以为互不相干、其实有关联），要等一个**脚印可能是错的**工具出现。到了。

远程工具碰了什么？工作发生在另一个进程里，这边看不见。server 唯一告诉我们的，是那个 `readOnlyHint`——"我是只读的"。**这是一句承诺，不是一个算出来的事实。**

```python
    def footprint_of(self, call: ToolCall) -> Footprint:
        """What a remote call touches -- as far as anyone here can honestly say.

        Chapter 8's rule was that a footprint must be resolved, not guessed,
        and this is the first tool set where that is impossible: the work
        happens in another process, on machines this one cannot see.

        The one thing a server does tell us is `annotations.readOnlyHint`, and
        that is a promise rather than an observation.  It is trusted for
        exactly one conclusion -- two read-only calls *to the same server* may
        overlap -- and for nothing else.  In particular a read-only remote tool
        is **not** given an empty footprint: it may still read a file this
        turn's `apply_patch` is writing, and nothing here can prove otherwise.
        Anything without the hint is `STATEFUL`, which is what chapter 8's
        default already said about tools it did not recognise.
        """
        registration = self.registrations.get(call.name)
        if registration is None or registration.tool.read_only is not True:
            return STATEFUL
        return Footprint(reads=frozenset({f"mcp:{registration.tool.server}"}))

def route_footprint(registry: McpRegistry, local: FootprintFn) -> FootprintFn:
    """One `footprint_of` for chapter 8, over both kinds of tool.

    A function rather than teaching either side about the other: `tools.py`
    has no business knowing what MCP is, and the registry has no business
    knowing where this repository's root is.
    """

    def footprint(call: ToolCall) -> Footprint:
        return registry.footprint_of(call) if is_remote(call.name) else local(call)

    return footprint
```

> - **信它，但只信到一个结论为止**：自称只读的工具，脚印是"读 `mcp:server名`"——意思正好是"**同一个 server 上的两个只读调用可以重叠**"，一个字不多。
>   没有这个标记的，一律 `STATEFUL`。
> - **它换不来一个空的脚印。** "只读"是对它自己那边说的；它完全可能读的正是本地这一轮 `apply_patch` 正在写的文件。第 8 章说过，空的脚印是唯一一句猜错了后果最严重的话——
>   这里第一次有人想让你说这句话。
> - **`route_footprint(registry, local)`**：本地工具的脚印由 `tools.footprint_of` 算，远程的由注册表算，按名字的开头分流。
>   用一个函数把两边接起来，而不是让它们互相认识：`tools.py` 不该知道 MCP 是什么，注册表不该知道仓库在哪。

```python
async def test_F09_footprint_of_a_remote_tool_without_a_hint_is_stateful() -> None:
    registry, clients = await started(files())
    try:
        assert registry.footprint_of(a_call("mcp__files__search", query="x")) == STATEFUL
    finally:
        await stop(clients)


async def test_F09_footprint_of_a_read_only_tool_allows_only_read_read_overlap() -> None:
    """`readOnlyHint` is trusted for one conclusion and no others.

    It is a promise from a process this one cannot inspect, so it buys "two
    read-only calls to the same server may overlap".  It does not buy an
    empty `Footprint()`: a remote tool may still touch a file this turn's
    `apply_patch` is writing, and nothing here can prove it does not.
    """
    registry, clients = await started(files())
    try:
        read_only = registry.footprint_of(a_call("mcp__files__stat", name="a"))
        assert read_only == Footprint(reads=frozenset({"mcp:files"}))
        assert not conflicts(read_only, read_only)
        assert conflicts(read_only, STATEFUL)
        assert read_only != Footprint()
    finally:
        await stop(clients)


async def test_F09_two_read_only_calls_to_one_server_share_a_batch() -> None:
    registry, clients = await started(files())
    try:
        plan = batches(
            [
                ToolCall("c1", "mcp__files__stat", {"name": "a"}, "{}"),
                ToolCall("c2", "mcp__files__stat", {"name": "b"}, "{}"),
                ToolCall("c3", "mcp__files__search", {"query": "x"}, "{}"),
            ],
            registry.footprint_of,
        )
        assert [[c.call_id for c in batch] for batch in plan] == [["c1", "c2"], ["c3"]]
    finally:
        await stop(clients)


def test_F09_route_footprint_sends_local_names_to_the_local_classifier() -> None:
    registry = McpRegistry()
    seen: list[str] = []

    def local(c: ToolCall) -> Footprint:
        seen.append(c.name)
        return Footprint(reads=frozenset({"local"}))

    route = route_footprint(registry, local)
    assert route(a_call("read_file", path="a.py")) == Footprint(reads=frozenset({"local"}))
    assert route(a_call("mcp__x__y")) == STATEFUL
    assert seen == ["read_file"]  # the remote call never reached the local one
```

---

## §15 子进程能看见什么——和一处留了很久的残留

第 2 章给 shell 的子进程用了环境变量白名单，理由是：我们和模型通话用的 API key 在环境变量里，子进程没有理由看到它。MCP server 是别人的程序，更是如此。

`mcp.py` 里的 `_subprocess_env` 分两步：

1. **只把白名单上的变量从自己的环境里复制出来**，再加上配置文件里为这个 server 明确写的那些（server 常常真的需要一个它自己的凭据，所以可以一条一条加）。
2. **把 SDK 会自己加上的那些变量置空。**

第二步是量出来的。只做第一步，把五个变量交给 SDK，然后问 server 它实际看见了什么：

```
we chose        : ['HOME', 'PATH', 'PATHEXT', 'SYSTEMROOT', 'TERM']
child saw       : ['APPDATA', 'HOME', 'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA', 'PATH', 'PATHEXT', 'PROCESSOR_ARCHITECTURE', 'SYSTEMDRIVE', 'SYSTEMROOT', 'TEMP', 'TERM', 'USERNAME', 'USERPROFILE']
  secret leaked : False
```

交出去五个，它收到十四个。SDK 把你给的和它自己的一份默认列表**合并**了——**你给的是下限，不是上限。** 多出来的九个里有 `USERNAME`、`USERPROFILE`、`APPDATA`：谁在跑这个进程、他的文件在哪。
好消息是有边界：放在环境里的一个假密钥没有漏过去（合并的是一份固定的列表，不是整个环境）。但"有边界"和"是你选的"是两回事。

合并只留了一种覆盖的办法：把不想给的变量**明确设成空的**。

```
after blanking  : ['APPDATA=(empty)', 'HOMEDRIVE=(empty)', 'HOMEPATH=(empty)', 'LOCALAPPDATA=(empty)', 'PROCESSOR_ARCHITECTURE=(empty)', 'SYSTEMDRIVE=(empty)', 'TEMP=(empty)', 'USERNAME=(empty)', 'USERPROFILE=(empty)']
```

server 仍然看得见 `USERNAME` 这个名字，但看不到是谁。

> **你没有选的默认值，也是你发布出去的决定。** 依赖不是托管。

### 15.1 意外：这个快照里，白名单那一行一直不在

改写这一章时，这个快照的测试是红的——在 Windows 和 Linux 上都是：

```
FAILED tests/test_faults_ch09.py::test_a_server_subprocess_does_not_inherit_the_hosts_api_key
E       AssertionError: assert 'OPENAI_API_KEY' not in {'AI_AGENT': ..., 'ALLUSERSPROFILE': 'C:\\ProgramData', 'APPDATA': ...}
```

去看 `_subprocess_env`，第一步不是"按白名单复制"，而是：

```python
    env = dict(os.environ)
```

**整个环境**，原样交给每一个 server，包括 API key。而它上面的 docstring 还在讲"白名单决定什么可以被复制出去"。

这一行是哪来的？这一章的变异脚本（§18）里有一条变异，标签是"把宿主的环境交给每个 server"，做的事情正是把那行白名单换成 `dict(os.environ)`。
变异脚本改完源码、跑完测试会改回去——**除非它在中间被强行结束**。它自带一个开工前的检查，对着当时的代码跑一下：

```
$ uv run python probe_mutations_ch09.py
refusing to run: the working tree is already mutated

  mcp.py: looks like 'the host environment is handed to every server' is still applied

Restore it (git checkout / re-copy) before running this again.
```

它自己认出了自己留下的东西。那次被打断的变异，就这样留在了这一章的代码快照里，并且被提交了；后面每一章的快照里这一行都是对的，只有这一章不是。

三件事值得记：

- **守着它的测试一直在，而且一直是红的。** 没有人看到，因为这个快照的测试没有被重新跑过。一个没人跑的测试，和没有这个测试一样。
- **那个开工前的检查是有用的**，但它只在有人再次运行变异脚本时才起作用。
- 第 6 章说过："一个会改你源码的工具，就是一个会把你的源码改坏的工具"，并且把"在临时拷贝里做变异"记成了一个没还的债。**这就是那笔债的利息。**
  这次改写里，所有变异测试都是在临时拷贝里跑的。

改回来之后：

```python
def _subprocess_env(config: ServerConfig) -> dict[str, str]:
    """What the server process is allowed to see.

    Two passes, and the order between them is what makes the result an upper
    bound rather than a lower one.

    The comprehension is F02-09 one process further out: the allowlist decides
    what may be copied out of *this* process, because an MCP server is
    somebody else's program, started by us, with no use for the key this agent
    talks to its model with.

    The blanking pass is F09-10, and it exists only because the SDK unions its
    own `DEFAULT_INHERITED_ENV_VARS` into whatever it is given -- so an
    allowlist handed over as-is would be a floor, not a ceiling.  Naming each
    unwanted key with an empty value is the only override the union permits.
    """
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.update(config.env)
    for key in DEFAULT_INHERITED_ENV_VARS:
        if key not in env:
            env[key] = ""
    return env
```

```python
async def test_a_server_subprocess_does_not_inherit_the_hosts_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chapter 2's allowlist (F02-09), one process further out.

    An MCP server is somebody else's program, started by us, and it does not
    need the key this agent talks to its model with.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
    from minicodex.mcp import _subprocess_env

    env = _subprocess_env(files())
    assert "OPENAI_API_KEY" not in env
    assert "PATH" in env

    with_credential = _subprocess_env(files(NOTES_TOKEN="explicitly-passed"))
    assert with_credential["NOTES_TOKEN"] == "explicitly-passed"
    assert "OPENAI_API_KEY" not in with_credential
```

> 把一个假的 `OPENAI_API_KEY` 放进环境（`monkeypatch.setenv`），子进程的环境里不能有它，但要有 `PATH`；配置里明确给的变量要在。

```bash
git add src/minicodex/mcp.py src/minicodex/registry.py tests/test_faults_ch09.py
git commit -m "feat(registry): retire tools with a reason, answer servers through the approver, and choose what a server may see"
```

---

## §16 在命令行里打开它

### 16.1 配置文件

```python
def load_config(path: Path) -> list[ServerConfig]:
    """Read `{"servers": {"name": {"command": [...], "env": {...}}}}`.

    A file rather than a flag, and JSON rather than anything cleverer, because
    it is the same shape codex uses (`mcp_servers` in its config) and because
    a command line with three servers on it is a command line nobody types
    twice.  Every field except `command` is optional.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    servers = raw.get("servers")
    if not isinstance(servers, dict):
        raise McpError(f"{path}: expected an object with a 'servers' key")
    configs = []
    for name, spec in servers.items():
        command = (spec or {}).get("command")
        if not isinstance(command, list) or not command:
            raise McpError(f"{path}: server {name!r} has no 'command' list")
        configs.append(
            ServerConfig(
                name=name,
                command=tuple(str(part) for part in command),
                env={str(k): str(v) for k, v in ((spec or {}).get("env") or {}).items()},
                cwd=(spec or {}).get("cwd"),
                startup_timeout=float((spec or {}).get("startup_timeout", 30.0)),
                tool_timeout=float((spec or {}).get("tool_timeout", 60.0)),
            )
        )
    return configs
```

> 读一个 JSON 文件：`{"servers": {"名字": {"command": [...], "env": {...}}}}`。用文件而不是命令行参数，因为带着三个 server 的一行命令没人愿意敲第二遍。
> 除了 `command`，别的都可以不写。形状不对时抛 `McpError`，并说出是哪个文件、哪个 server。

仓库里带一份示例，`mcp.example.json`：

```json
{
  "servers": {
    "files": {"command": ["python", "mcp_servers/files_server.py"]},
    "notes": {"command": ["python", "mcp_servers/notes_server.py"]}
  }
}
```

```python
def test_load_config_reads_the_shape_the_readme_documents(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "servers": {
                    "notes": {
                        "command": ["python", "notes.py"],
                        "env": {"TOKEN": "x"},
                        "tool_timeout": 5,
                    }
                }
            }
        )
    )
    (server,) = load_config(path)
    assert server.name == "notes"
    assert server.command == ("python", "notes.py")
    assert server.env == {"TOKEN": "x"}
    assert server.tool_timeout == 5.0


def test_load_config_refuses_a_server_with_no_command(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {"notes": {}}}))
    with pytest.raises(McpError, match="no 'command' list"):
        load_config(path)
```

### 16.2 `__main__.py` 的改动

开头多了两组导入：`from minicodex.mcp import McpError`，和 `from minicodex.registry import McpRegistry, connect, elicitation_handler, load_config, route_footprint`。

`ask` 子命令多一个参数：

```python
    ask.add_argument(
        "--mcp",
        type=Path,
        default=None,
        metavar="CONFIG",
        help=(
            "JSON file listing MCP servers to start: "
            '{"servers": {"notes": {"command": ["python", "server.py"]}}}'
        ),
    )
```

`main()` 里，在启动任何东西之前先把配置读一遍：

```python
        if args.mcp is not None:
            # Read once here, before anything is started, so that a typo in
            # the file is one line on stderr rather than a traceback from
            # inside the event loop.
            try:
                load_config(args.mcp)
            except (OSError, ValueError, McpError) as exc:
                print(f"--mcp: {exc}", file=sys.stderr)
                return 1
```

> 文件里的一个笔误，应该是屏幕上的一行 `--mcp: ...`，而不是从事件循环深处冒出来的一屏报错。`_ask` 也多收一个参数 `mcp_config=args.mcp`。

`_ask()` 里，在创建模型客户端**之前**：

```python
    # The registry owns the whole tool list, this project's own tools included,
    # because `llm.tools` is that same list object: `tool_search` reveals a
    # schema by appending to it, and the next request picks the change up
    # without anything having to be rebuilt or re-passed.
    registry = McpRegistry(local=list(TOOL_SCHEMAS))
    clients = []
    if mcp_config is not None:
        clients = await connect(
            load_config(mcp_config),
            registry,
            handlers={"elicitation/create": elicitation_handler(session)},
        )
        for name, reason in registry.failures.items():
            print(f"[mcp: {name} unavailable -- {reason}]", file=sys.stderr)
        for client in clients:
            offered = sum(1 for r in registry.registrations.values() if r.client is client)
            print(f"[mcp: {client.config.name} connected, {offered} tool(s)]")
        if registry.deferred:
            print(f"[mcp: {len(registry.deferred)} tool(s) deferred behind tool_search]")

    llm = ChatCompletionsModel(
        base_url=base_url or default_url,
        model=model or default_model,
        # Read from the environment, never from a flag: a key in argv shows up
        # in shell history and in `ps`.
        api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
        tools=registry.visible,
    )
```

> - `McpRegistry(local=list(TOOL_SCHEMAS))`：注册表管**整张**工具列表，连我们自己的四个在内。
> - 给了 `--mcp` 才去连。连不上的在 stderr 上各说一行；连上的各说一行"几个工具"；有被藏起来的，也说一行。
> - **`tools=registry.visible`**——§7.5 说的那一行。交出去的是列表本身，不是拷贝。

创建 `Agent` 之前和之后：

```python
    writer = RolloutWriter(rollout_path(current.session_id, session_dir), current)
    root = Path.cwd().resolve()
    handlers = {**default_tools(root=root, session=session), **registry.handlers()}
    if registry.deferred:
        handlers["tool_search"] = registry.search_handler()
```

```python
        footprint_of=route_footprint(registry, functools.partial(footprint_of, root=root)),
    )

    try:
        result = await agent.run(question)
    finally:
        # Servers are subprocesses this process started, and chapter 2 already
        # paid for the lesson about leaving those behind (F02-08).  In a
        # `finally`, because the interesting exits are the ones that were not
        # planned.
        for client in clients:
            await client.close()
```

> - 处理函数表：自己的工具，加上注册表的。**只有在会话开始时就有工具被藏起来，才放 `tool_search` 进去**——这就是 §11.2 说"有没有 `tool_search` 是会话开始时定的"的那个地方。
> - `footprint_of=route_footprint(...)`：§14。
> - `try ... finally`：server 是我们启动的子进程。第 2 章已经为"把子进程留在身后"付过学费；放在 `finally` 里，因为值得担心的退出，都是没计划过的那些。

第 8 章学到过：一个机制写对了、测过了，而命令行里接它的那一行没人守着。这里同样的一行是 `tools=registry.visible`：

```python
def test_the_cli_hands_the_model_client_the_registrys_own_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tool_search` reveals a tool by appending to `registry.visible`, and the
    next request shows it only if the model client is holding that same list.

    The registry-level test above pins the append.  This one pins the line in
    `__main__.py` that makes the append matter: written as
    `tools=list(registry.visible)` it runs, reports "loaded, callable now",
    and the model never sees the tool.  Same shape as chapter 8's scheduler
    that was correct, tested, and switched off.
    """
    import minicodex.__main__ as cli
    from minicodex.agent import Agent, RunResult
    from minicodex.history import History

    seen: dict[str, Any] = {}
    made: list[McpRegistry] = []

    class SpyRegistry(McpRegistry):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    class SpyAgent(Agent):
        def __init__(self, llm: Any, handlers: Any, **kwargs: Any) -> None:
            seen["llm"] = llm
            seen["handlers"] = handlers
            super().__init__(llm, handlers, **kwargs)

        async def run(self, user_message: str) -> RunResult:
            return RunResult("ok", "completed", 1, History())

    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(
        json.dumps(
            {"servers": {"notes": {"command": [sys.executable, str(SERVERS / "notes_server.py")]}}}
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "McpRegistry", SpyRegistry)
    monkeypatch.setattr(cli, "Agent", SpyAgent)

    code = cli.main(
        ["ask", "anything", "--mcp", str(mcp_json), "--session-dir", str(tmp_path / "sessions")]
    )

    assert code == 0
    (registry,) = made
    assert seen["llm"].tools is registry.visible, "the model client was given a copy"
    assert "mcp__notes__search" in seen["handlers"]
```

> 真的调用 `main()`，带一份只有 `notes` 的配置；`SpyRegistry` 记下被创建的注册表，`SpyAgent` 记下交给 Agent 的模型客户端和处理函数表（`run` 换成直接返回，不联网）。
> 断言用的是 `is`，不是 `==`：**是同一个列表对象**，不是内容相同的两个。

### 16.3 真的跑一次

```
$ uv run minicodex ask "Search my notes for the word tool and tell me the name of the note that matches." --provider openai --mcp mcp.example.json --yes
[mcp: files connected, 2 tool(s)]
[mcp: notes connected, 3 tool(s)]
The note that matches the search for the word "tool" is titled "idea."

[gpt-4o-mini | completed after 2 turn(s)]
[sandbox_mode=read-only, approval_policy=on-request]
[tokens: x0.67 from 2 observation(s)]
[transcript: .minicodex\recordings\session-1790827388.jsonl]
[session: .minicodex\sessions\20261001T120309-33304.jsonl  (resume with: minicodex ask ... --resume last)]
```

（Windows，2026-10-01，gpt-4o-mini。）两个 server，五个工具，装得下预算，所以没有 `tool_search`。模型第一轮调用了 `mcp__notes__search`，第二轮回答。

同一次实验，前一次的问法是"哪些笔记提到了 tools"，得到的回答是"没有笔记提到 tools"——那条笔记里写的是 `tool`，而 `notes` 的搜索是原样匹配。
**工具是别人的，它怎么匹配由它决定**；模型把一句"没有匹配"如实转告了。

```bash
git add src/minicodex/__main__.py src/minicodex/registry.py mcp.example.json tests/test_faults_ch09.py
git commit -m "feat(cli): --mcp starts the configured servers and hands their tools to the model"
```

---

## §17 没有复现的一条：F09-09，和整份探针

清单上最后一条：模型一次只调用一个工具，十件事要十次往返，应该做一个"批量模式"。量一下：

```
$ uv run python probe_mcp.py roundtrips
=== F09-09: batching, gpt-4o-mini ===
    --- the ten targets are known up front ---
      10 call(s) in the first response
      10 call(s) in the first response
      10 call(s) in the first response
    --- the targets have to be discovered first ---
      2 round trip(s), 11 call(s) total
      2 round trip(s), 11 call(s) total
      2 round trip(s), 11 call(s) total
```

十个目标都已知时：**第一次回复里就是十个调用**，3/3。要先查出目标是哪些的版本：**两次往返**（先列出，再十个一起），3/3。

**F09-09 没有复现。** 模型本来就会在一次回复里发多个调用，第 8 章的调度器让它们一起跑——清单想要的东西已经付过钱了。所以没有做"批量模式"。

整份探针，`probe_mcp.py`：

```python
"""What chapter 9 measured, and how.

Run one section at a time:

    uv run python probe_mcp.py collide      # F09-01, no network
    uv run python probe_mcp.py tokens       # F09-03, no network
    uv run python probe_mcp.py names        # tool-name limits, real API
    uv run python probe_mcp.py selection    # F09-02, real API, ~30 requests
    uv run python probe_mcp.py roundtrips   # F09-09, real API

Anything with "real API" costs money and needs OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx

from minicodex.registry import McpRegistry, model_name
from minicodex.tokens import estimate_messages

MODEL = "gpt-4o-mini"
URL = "https://api.openai.com/v1/chat/completions"
SAMPLES = 3


def _key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set; this section needs it.", file=sys.stderr)
        raise SystemExit(1)
    return key


async def _ask(
    client: httpx.AsyncClient,
    tools: list[dict[str, Any]],
    prompt: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One non-streaming request.  Returns the tool calls it asked for."""
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt,
        "tools": tools,
        "temperature": 1,
    }
    # A dropped connection is not a measurement.  Without this, one failed TLS
    # handshake forty requests into `selection` throws the whole section away
    # -- which happened, twice, on the machine these numbers come from.
    for attempt in range(8):
        try:
            resp = await client.post(
                URL, json=body, headers={"Authorization": f"Bearer {_key()}"}, timeout=90
            )
            break
        except httpx.TransportError:
            if attempt == 7:
                raise
            await asyncio.sleep(2)
    if resp.status_code != 200:
        return [{"__http__": resp.status_code, "__body__": resp.text}]
    message = resp.json()["choices"][0]["message"]
    return [
        {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}
        for c in (message.get("tool_calls") or [])
    ]


# -- fake catalogues, shaped like real ones ---------------------------------


# Round 1 used sixty tools called `operation_NN` and the model found the needle
# 3/3 at every size.  That measured almost nothing: no distractor was about
# notes, searching, meetings or text, so "pick the only relevant tool" was the
# task, not "pick the right one among relevant ones".
#
# Round 2 made every distractor plausible -- and repeated ten of them six times
# each, which turned out to be its own artefact: `tool_search("meeting")`
# returned five *copies* of one tool.  Round 3 is sixty distinct tools across
# six servers, which is what a machine with six MCP servers configured actually
# looks like.
_DOCS = [
    ("search_documents", "Full-text search across indexed documents and return matching passages."),
    ("get_document", "Fetch one document by its exact identifier."),
    ("list_documents", "List indexed documents with their titles."),
    ("index_document", "Add a document to the search index."),
    ("export_document", "Export a document as PDF."),
    ("diff_documents", "Show the differences between two document revisions."),
    ("tag_document", "Attach a tag to a document."),
    ("share_document", "Create a shareable link for a document."),
    ("archive_document", "Move a document out of the active set."),
    ("restore_document", "Bring an archived document back."),
]
_CAL = [
    ("find_meeting", "Look up a meeting by title, attendee or date and return its record."),
    ("summarise_meeting", "Summarise a meeting given its identifier."),
    ("search_calendar", "Search calendar entries by keyword and return matching events."),
    ("create_event", "Add an event to the calendar."),
    ("cancel_event", "Cancel a calendar event and notify attendees."),
    ("list_attendees", "List everyone invited to an event."),
    ("find_free_slot", "Find a time when everyone is available."),
    ("reschedule_event", "Move an event to a new time."),
    ("get_agenda", "Return the agenda attached to a meeting."),
    ("record_minutes", "Save minutes against a meeting record."),
]
_MEM = [
    ("recall_memory", "Search remembered facts from previous sessions by keyword."),
    ("store_memory", "Remember a fact for future sessions."),
    ("forget_memory", "Delete a remembered fact."),
    ("list_memories", "List everything remembered about this project."),
    ("summarise_memories", "Summarise what is remembered about a topic."),
    ("link_memories", "Record that two remembered facts are related."),
    ("pin_memory", "Mark a remembered fact as always relevant."),
    ("expire_memory", "Set an expiry date on a remembered fact."),
    ("export_memories", "Export all remembered facts as JSON."),
    ("import_memories", "Load remembered facts from a JSON file."),
]
_CHAT = [
    ("search_messages", "Search chat messages by keyword and return the matching thread."),
    ("post_message", "Post a message to a channel."),
    ("list_channels", "List the channels this workspace has."),
    ("get_thread", "Fetch one conversation thread by id."),
    ("react_to_message", "Add an emoji reaction to a message."),
    ("pin_message", "Pin a message in its channel."),
    ("invite_user", "Invite someone to a channel."),
    ("mute_channel", "Stop notifications from a channel."),
    ("upload_file", "Upload a file to a channel."),
    ("search_files", "Search files shared in chat by name."),
]
_CODE = [
    ("grep_workspace", "Search the workspace for a literal string and return matching lines."),
    ("open_pull_request", "Open a pull request from a branch."),
    ("list_branches", "List the branches in a repository."),
    ("review_diff", "Return the diff of a pull request."),
    ("run_pipeline", "Trigger a CI pipeline for a branch."),
    ("get_build_log", "Fetch the log of one build."),
    ("list_issues", "List open issues in a repository."),
    ("comment_on_issue", "Add a comment to an issue."),
    ("assign_issue", "Assign an issue to somebody."),
    ("close_issue", "Close an issue with a reason."),
]
_WIKI = [
    ("query_notes_index", "Query the notes index and return note identifiers ranked by relevance."),
    ("get_note", "Fetch one note by its exact identifier."),
    ("list_notes", "List every saved note with its name and first line."),
    ("create_page", "Create a new wiki page."),
    ("edit_page", "Edit an existing wiki page."),
    ("search_wiki", "Search wiki pages by keyword."),
    ("list_spaces", "List the wiki spaces available."),
    ("watch_page", "Get notified when a page changes."),
    ("page_history", "Return the revision history of a page."),
    ("move_page", "Move a page to another space."),
]
CATALOGUE = [
    ("docs", _DOCS),
    ("calendar", _CAL),
    ("memory", _MEM),
    ("chat", _CHAT),
    ("code", _CODE),
    ("wiki", _WIKI),
]
NEAR_MISSES = [(server, name, desc) for server, tools in CATALOGUE for name, desc in tools]


def _catalogue(count: int, *, hard: bool = False) -> list[dict[str, Any]]:
    """`count` tools with the schema shape a real MCP server produces."""
    tools = []
    for index in range(count):
        if hard:
            server, base, description = NEAR_MISSES[index % len(NEAR_MISSES)]
            name = model_name(server, base)
        else:
            name = model_name("bigcorp", f"operation_{index:02d}")
            description = (
                f"Run operation {index} against the configured backend and return "
                "a structured report describing every record it touched."
            )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "required": ["target"],
                        "properties": {
                            "target": {
                                "type": "string",
                                "description": "Identifier of the object to operate on.",
                            },
                            "dry_run": {
                                "type": "boolean",
                                "description": "Report what would happen without doing it.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of records to touch.",
                            },
                        },
                    },
                },
            }
        )
    return tools


NEEDLE = {
    "type": "function",
    "function": {
        "name": model_name("notes", "search"),
        "description": "Search saved notes by keyword and return the matching note.",
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {"query": {"type": "string", "description": "Keyword."}},
        },
    },
}

TASK = "Find the note about the meeting. Use a tool."


# -- sections ----------------------------------------------------------------


def collide() -> None:
    """F09-01, with no server and no network: it is a dict, and dicts overwrite."""
    print("=== F09-01: two servers, one table ===")
    naive: dict[str, str] = {}
    for server, tools in (("files", ["search", "stat"]), ("notes", ["search", "render"])):
        for tool in tools:
            naive[tool] = server
    print(f"    naive dict:      {len(naive)} tools from 4 declared -> {naive}")

    registry = McpRegistry()
    names = [
        model_name(server, tool)
        for server, tools in (("files", ["search", "stat"]), ("notes", ["search", "render"]))
        for tool in tools
    ]
    print(f"    namespaced:      {len(set(names))} tools -> {sorted(set(names))}")
    print(f"    registry prefix: {registry.__class__.__name__} uses {model_name('S', 'T')}")


def tokens() -> None:
    """F09-03: what the tool list costs, per turn, before anyone says anything."""
    print("=== F09-03: schema cost, measured with chapter 6's estimator ===")
    conversation = [
        {"role": "system", "content": "You are a coding agent working in a repository."},
        {"role": "user", "content": "Find the note about the meeting and summarise it."},
    ]
    baseline = estimate_messages(conversation, [])
    print(f"    conversation alone:                       {baseline:>6} tokens")
    for count in (4, 12, 30, 60, 120):
        catalogue = _catalogue(count)
        total = estimate_messages(conversation, catalogue)
        schema_cost = total - baseline
        share = 100 * schema_cost / total
        print(
            f"    + {count:>3} tool schemas: {schema_cost:>6} tokens  "
            f"({share:.1f}% of the request, {schema_cost / count:.0f} per tool)"
        )
    deferred = estimate_messages(conversation, [NEEDLE])
    print(f"    deferred (1 search tool only):            {deferred - baseline:>6} tokens")
    print("    Every number above is paid on every turn, whether or not a tool is used.")


async def names() -> None:
    """What the providers actually accept as a function name."""
    print("=== tool-name limits, measured against the real API ===")
    async with httpx.AsyncClient() as client:
        for label, name in (
            ("64 chars", "a" * 64),
            ("65 chars", "a" * 65),
            ("128 chars", "a" * 128),
            ("256 chars", "a" * 256),
            ("512 chars", "a" * 512),
            ("dots", "mcp.notes.search"),
            ("spaces", "mcp notes search"),
            ("slash", "mcp/notes/search"),
            ("double underscore", model_name("notes", "search")),
        ):
            tool = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "A tool.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            calls = await _ask(client, [tool], "Call the tool.")
            first = calls[0] if calls else {}
            if "__http__" in first:
                status = first["__http__"]
                try:
                    detail = json.loads(first["__body__"])["error"]["message"]
                except (ValueError, KeyError):
                    detail = first["__body__"]
                print(f"    {label:<20} HTTP {status}: {detail[:190]}")
            else:
                print(f"    {label:<20} accepted, model called {first.get('name', '(nothing)')!r}")


async def selection() -> None:
    """F09-02: does a big catalogue actually degrade the choice?"""
    print(f"=== F09-02: finding one tool among N ({MODEL}, {SAMPLES} samples each) ===")
    async with httpx.AsyncClient() as client:
        for hard in (False, True):
            print(f"    --- distractors: {'plausible near-misses' if hard else 'irrelevant'} ---")
            for count in (0, 8, 30, 60):
                catalogue = [*_catalogue(count, hard=hard), NEEDLE]
                hits = 0
                wrong: list[str] = []
                for _ in range(SAMPLES):
                    calls = await _ask(client, catalogue, TASK)
                    chosen = calls[0]["name"] if calls else "(no tool call)"
                    if chosen == NEEDLE["function"]["name"]:
                        hits += 1
                    else:
                        wrong.append(chosen)
                noise = f"   picked instead: {', '.join(sorted(set(wrong)))}" if wrong else ""
                print(f"      {count + 1:>3} tools offered:  correct {hits}/{SAMPLES}{noise}")

        # If the variable is really "is there a plausible alternative" and not
        # "how many tools are there", then chapter 3's fix for two overlapping
        # tools (F03-04/F03-05: say what a tool is *not* for) should work at
        # sixty just as it worked at two.
        print("    --- 61 hard tools, needle carries a chapter-3 disambiguating clause ---")
        sharpened = {
            "type": "function",
            "function": {
                **NEEDLE["function"],
                "description": (
                    "Search saved notes by keyword and return the matching note. This is "
                    "the only tool that reads the user's own saved notes. Do not use the "
                    "document, calendar, message or memory search tools for a note."
                ),
            },
        }
        for count in (8, 60):
            catalogue = [*_catalogue(count, hard=True), sharpened]
            hits = 0
            wrong = []
            for _ in range(SAMPLES):
                calls = await _ask(client, catalogue, TASK)
                chosen = calls[0]["name"] if calls else "(no tool call)"
                if chosen == NEEDLE["function"]["name"]:
                    hits += 1
                else:
                    wrong.append(chosen)
            noise = f"   picked instead: {', '.join(sorted(set(wrong)))}" if wrong else ""
            print(f"      {count + 1:>3} tools offered:  correct {hits}/{SAMPLES}{noise}")

        print("    --- the same 61 hard tools, deferred behind tool_search ---")
        searched = revealed_hits = 0
        for _ in range(SAMPLES):
            registry = _deferred_registry(hard=True)
            first = await _ask(client, [registry.search_schema()], TASK)
            if not first or first[0]["name"] != "tool_search":
                print(f"      turn 1: model did not search, it called {first}")
                continue
            searched += 1
            query = json.loads(first[0]["arguments"]).get("query", "")
            output = registry.search(query, 5)
            revealed = [s["function"]["name"] for s in registry.visible]
            # The real message sequence, not the search result pasted into a
            # user turn: the first version of this probe did the latter, and
            # "the model made no tool call" is exactly the artefact a wrong
            # message shape produces.
            second = await _ask(
                client,
                [registry.search_schema(), *registry.visible],
                [
                    {"role": "user", "content": TASK},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "tool_search",
                                    "arguments": first[0]["arguments"],
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": output},
                ],
            )
            chosen = second[0]["name"] if second else "(no tool call)"
            ok = chosen == NEEDLE["function"]["name"]
            revealed_hits += ok
            print(
                f"      query={query!r:<44} revealed {len(revealed)} "
                f"-> {'correct' if ok else chosen}"
            )
        print(f"      searched {searched}/{SAMPLES}, then correct {revealed_hits}/{SAMPLES}")


def _deferred_registry(*, hard: bool) -> McpRegistry:
    """A registry holding the probe's fake catalogue, everything deferred.

    Built by hand rather than from a server: this section is about what the
    model does with a tool list, and starting sixty subprocesses to measure
    that would be measuring something else.
    """
    from minicodex.mcp import RemoteTool

    registry = McpRegistry(schema_budget=0)
    tools = []
    for schema in [*_catalogue(60, hard=hard), NEEDLE]:
        function = schema["function"]
        server, _, bare = function["name"].removeprefix("mcp__").partition("__")
        tools.append(RemoteTool(server, bare, function["description"], function["parameters"]))
    registry.registrations = {}
    registry.add(None, tools)  # type: ignore[arg-type]
    return registry


async def roundtrips() -> None:
    """F09-09: how many turns does a ten-item batch job take?"""
    print(f"=== F09-09: batching, {MODEL} ===")
    tool = {
        "type": "function",
        "function": {
            "name": model_name("files", "stat"),
            "description": "Return the size in bytes of one indexed file.",
            "parameters": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        },
    }
    files = [f"chapter_{i:02d}.md" for i in range(10)]
    prompt = "Get the size of every one of these files: " + ", ".join(files)
    async with httpx.AsyncClient() as client:
        print("    --- the ten targets are known up front ---")
        for _ in range(SAMPLES):
            calls = await _ask(client, [tool], prompt)
            print(f"      {len(calls)} call(s) in the first response")

        # The version that cannot be batched: the model does not know what to
        # ask for until a previous call answers.  This is the shape F09-09 is
        # actually about, and it is the shape parallel tool calls cannot help.
        print("    --- the targets have to be discovered first ---")
        listing = {
            "type": "function",
            "function": {
                "name": model_name("files", "list"),
                "description": "List the names of every indexed file.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for _ in range(SAMPLES):
            messages: list[dict[str, Any]] = [
                {
                    "role": "user",
                    "content": "Find the largest indexed file. Report its name and size.",
                }
            ]
            turns = total = 0
            for turn in range(12):
                calls = await _ask(client, [listing, tool], messages)
                if not calls or "__http__" in calls[0]:
                    break
                turns = turn + 1
                total += len(calls)
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"c{turn}_{i}",
                                "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]},
                            }
                            for i, c in enumerate(calls)
                        ],
                    }
                )
                for i, call in enumerate(calls):
                    if call["name"].endswith("__list"):
                        answer = "\n".join(files)
                    else:
                        name = json.loads(call["arguments"] or "{}").get("name", "")
                        answer = str(1000 + 37 * (hash(name) % 40))
                    messages.append(
                        {"role": "tool", "tool_call_id": f"c{turn}_{i}", "content": answer}
                    )
            print(f"      {turns} round trip(s), {total} call(s) total")


SECTIONS = {
    "collide": collide,
    "tokens": tokens,
    "names": names,
    "selection": selection,
    "roundtrips": roundtrips,
}


def main() -> None:
    chosen = sys.argv[1:] or ["collide", "tokens"]
    for name in chosen:
        section = SECTIONS[name]
        if asyncio.iscoroutinefunction(section):
            asyncio.run(section())
        else:
            section()
        print()


if __name__ == "__main__":
    main()
```

> - **`_ask(client, tools, prompt)`**：发一个不流式的请求，返回模型要求的工具调用。服务商拒绝了请求（比如工具名不合法）就返回状态码和正文，让调用的地方自己打印。
>   连接断了会重试——改写时加的：`selection` 跑到第四十个请求时一次握手失败，整段作废，发生了两次。**一次断线不是一次测量。**
> - **六份假目录**（`_DOCS`、`_CAL`、`_MEM`、`_CHAT`、`_CODE`、`_WIKI`）：§7.3 的那六十个各不相同的工具。上面的注释记着前两轮为什么不算数。
> - **`_catalogue(count, hard=...)`**：造 `count` 个工具。`hard=False` 是 `operation_NN`，`hard=True` 从那六份目录里取。每个都带三个参数，为的是大小像一个真的 MCP 工具。
> - **`NEEDLE`、`TASK`**：要找的那个工具，和问的那句话。
> - **五段**：`collide`（§5）、`tokens`（§6）、`names`（§5.1）、`selection`（§7）、`roundtrips`（这一节）。
>   `selection` 里"藏起来"那一组的第二个请求，发的是**真实的消息序列**（用户、助手的工具调用、工具结果），而不是把搜索结果贴进一条用户消息——注释里记着：
>   第一版是后一种写法，而"模型没有调用工具"恰好就是消息形状不对时会出现的假象。
> - **`_deferred_registry(hard=...)`**：手工造一个"全部藏起来"的注册表（`schema_budget=0`）。不启动六十个子进程：这一段量的是模型拿到一张工具列表后怎么做。
> - **`main()`**：不给参数时只跑不联网的两段。

---

## §18 逐条验证

### 18.1 全部测试，两个系统

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Windows（2026-10-01）：

```
All checks passed!
56 files already formatted
1369 passed, 9 skipped in 40.31s
```

Linux（WSL Ubuntu，Python 3.12，同一天）：

```
1378 passed in 30.55s
```

比第 8 章多 48 个：`test_faults_ch09.py` 的 47 个，加 `test_packaging.py` 里的 1 个（§18.3）。Windows 上跳过的 9 个，是前面几章里只在 POSIX 系统上才有意义的测试。

### 18.2 这些测试自己靠得住吗

第 6 章起，每一章都问一遍：把代码故意改错，测试会不会红。这一章把它固定成一个脚本，`probe_mutations_ch09.py`：

```python
"""Do chapter 9's tests fail when chapter 9's code is wrong?

A green suite proves nothing until you have seen it go red on purpose.  Each
entry below is a one-line edit that should break something; the script applies
it, runs the suite, restores the file, and reports how many tests noticed.

Restores from `atexit` and a signal handler, not from `finally` alone -- that
is chapter 6's lesson, learned by leaving `if False:` in `tokens.py` after a
Ctrl-C during a pytest run.
"""

from __future__ import annotations

import atexit
import contextlib
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    (
        "registry.py",
        "no namespacing: every server's tool keeps its own name",
        'return f"{PREFIX}{DELIMITER}{sanitize(server)}{DELIMITER}{sanitize(tool)}"',
        "return sanitize(tool)",
    ),
    (
        "registry.py",
        "isError is not mentioned to the model",
        r'text = f"The tool reported an error.\n{text}"',
        "text = text",
    ),
    (
        "registry.py",
        "an empty result renders as an empty string",
        r'text = "\n".join(part for part in parts if part) or "(the tool returned no content)"',
        r'text = "\n".join(part for part in parts if part)',
    ),
    (
        "registry.py",
        "a read-only remote tool is assumed to touch nothing",
        'return Footprint(reads=frozenset({f"mcp:{registration.tool.server}"}))',
        "return Footprint()",
    ),
    (
        "registry.py",
        "the schema budget ignores this project's own tools",
        "fits = estimate_messages((), self.local + schemas) <= self.schema_budget",
        "fits = estimate_messages((), schemas) <= self.schema_budget",
    ),
    (
        "registry.py",
        "tool_search reveals into a copy instead of the live list",
        "self.visible.append(self.schema_for(name))",
        "list(self.visible).append(self.schema_for(name))",
    ),
    (
        "registry.py",
        "a retired tool gets chapter 0's 'no tool named' treatment",
        'return f"{self.retired[name]}. Use one of the tools you were given instead."',
        'return "Use one of the tools you were given instead."',
    ),
    (
        "registry.py",
        "the deferred index is left out of tool_search's description",
        r'f"Tools available but not yet loaded:\n{self.index()}"',
        r'"Tools available but not yet loaded: (ask and find out)"',
    ),
    # The demultiplexing mutation that used to live here is gone with the code
    # it attacked: telling a response from a notification from a
    # server-initiated request is the SDK's job now. What is left to mutate is
    # what this project still decides for itself -- which is the honest test
    # of whether a dependency was the right call.
    (
        "mcp.py",
        "the server's stderr goes to the terminal, so a dead server cannot say why",
        "return stdio_client(params, errlog=handle)",
        "return stdio_client(params)",
    ),
    (
        "registry.py",
        "structuredContent that only repeats the text is emitted anyway",
        "        if not _echoes(structured, parts):\n            parts.append(",
        "        if True:\n            parts.append(",
    ),
    (
        "mcp.py",
        "no startup timeout",
        "asyncio.shield(opened), timeout=self.config.startup_timeout",
        "asyncio.shield(opened), timeout=None",
    ),
    (
        "registry.py",
        "a restarted server may add tools the loop has no handler for",
        "returning = [tool for tool in tools if model_name(tool.server, tool.name) in before]",
        "returning = list(tools)",
    ),
    (
        "registry.py",
        "a restart may start deferring behind a tool_search nothing answers to",
        "self._restage(defer=was_deferring)",
        "self._restage()",
    ),
    (
        "mcp.py",
        "the host environment is handed to every server",
        "env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}",
        "env = dict(os.environ)",
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = SRC / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def refuse_if_already_mutated() -> None:
    """Do not start on a tree somebody else's run left dirty.

    `atexit` and a `SIGINT` handler restore the source on every *graceful*
    exit, and neither runs on a `SIGKILL` -- so a probe killed by a timeout,
    a CI cancellation or an impatient `taskkill` leaves a mutation applied.

    That is not a hypothetical.  It happened here twice: the second time,
    the mutated `mcp.py` was copied to twelve downstream steps, and every one
    of them reported the *same* failing test.  A whole test sweep looked like
    a regression in the code and was an artefact of a dead process.

    The cheap guard is to look before writing.  The test is **both** halves --
    the original text missing *and* the mutated text present -- because
    `after` alone gives false positives: several mutations replace a specific
    expression with a simpler one that occurs legitimately elsewhere in the
    same file, and a guard that cries wolf is a guard somebody deletes.
    """
    dirty = [
        f"{name}: looks like {label!r} is still applied"
        for name, label, before, after in MUTATIONS
        if before not in ORIGINALS[name] and after in ORIGINALS[name]
    ]
    if dirty:
        print("refusing to run: the working tree is already mutated\n")
        for line in dirty:
            print(f"  {line}")
        print("\nRestore it (git checkout / re-copy) before running this again.")
        raise SystemExit(2)


def main() -> None:
    # Line-buffered even when stdout is a file.  Each mutation runs the whole
    # suite, so a full run is tens of minutes; with the default block
    # buffering that a redirect turns on, `probe > log.txt` shows *nothing*
    # until the process exits, and a run that has to be killed leaves no
    # record of how far it got -- which is exactly when you want one.
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        sys.stdout.reconfigure(line_buffering=True)

    refuse_if_already_mutated()
    print(f"{len(MUTATIONS)} mutations, tests/test_faults_ch09.py\n")
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
                [sys.executable, "-m", "pytest", "tests/test_faults_ch09.py", "-q", "--no-header"],
                capture_output=True,
                text=True,
            )
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

> - **`MUTATIONS`**：十四条，每条四样东西——哪个文件、一句说明、原来的文字、改成什么。
> - **`restore()`**，登记在 `atexit` 和 Ctrl-C 的处理函数上：正常退出、出错退出、被 Ctrl-C，都会把源码改回去。
> - **`refuse_if_already_mutated()`**：§15.1 里那个开工前的检查。判断要**两个条件同时成立**——原文不在了，**而且**改后的文字在。只看后一个会误报：
>   好几条变异是把一个具体的表达式换成更简单的，而那个更简单的写法在同一个文件别处本来就有。**一个会乱叫的检查，是一个迟早被人删掉的检查。**
> - **`main()`**：逐条——原文找不到就报 `could not apply`，**并且算作没抓到**；否则改、跑这一章的测试、改回去、数有几个测试红了。有任何一条没被抓到，以失败退出。

"找不到原文算作没抓到"这条规矩，改写时立刻用上了。把连接改成"每条一个任务"（§11.1）之后第一次跑：

```
  !! could not apply: the schema budget ignores this project's own tools
  !! could not apply: no startup timeout

2 mutation(s) nothing noticed:
```

两条变异要找的那行代码，已经被这次改写改成了别的样子。如果脚本把"没改成"当成"抓到了"，这两条守卫会从此悄悄失效，而报告上还是"全部抓到"。把两条的原文对上现在的代码之后：

```
$ uv run python probe_mutations_ch09.py
14 mutations, tests/test_faults_ch09.py

   23 test(s) fail  <-  no namespacing: every server's tool keeps its own name
    1 test(s) fail  <-  isError is not mentioned to the model
    1 test(s) fail  <-  an empty result renders as an empty string
    1 test(s) fail  <-  a read-only remote tool is assumed to touch nothing
    1 test(s) fail  <-  the schema budget ignores this project's own tools
    1 test(s) fail  <-  tool_search reveals into a copy instead of the live list
    1 test(s) fail  <-  a retired tool gets chapter 0's 'no tool named' treatment
    1 test(s) fail  <-  the deferred index is left out of tool_search's description
    1 test(s) fail  <-  the server's stderr goes to the terminal, so a dead server cannot say why
    6 test(s) fail  <-  structuredContent that only repeats the text is emitted anyway
    1 test(s) fail  <-  no startup timeout
    1 test(s) fail  <-  a restarted server may add tools the loop has no handler for
    1 test(s) fail  <-  a restart may start deferring behind a tool_search nothing answers to
    1 test(s) fail  <-  the host environment is handed to every server

every mutation was caught.
```

（Linux，在一份临时拷贝里跑的。）十四条都被抓到。第一条红了 23 个，因为几乎每个测试都要靠名字找到工具；其余大多只有一个测试守着——那一个就是为它写的。

### 18.3 它该不该进 CI

第 -1 章给 CI 写过一个测试：会挡住合并的那个流程，最多六步——"它得一直是快的"。把变异检查加成第七步，那个测试红了。

把 6 改成 7 是最省事的做法，也是不对的做法，因为它跳过了那个测试真正在问的问题：**这一步凭什么挡住一次合并？** 变异检查量的是**测试的质量**，不是这次改动对不对。
一个代码正确、但有一条变异没被抓到的 PR，应该被报告、被人看一眼，而不是被拦住。

所以是第二个流程，`.github/workflows/postmerge.yml`，只在合并到 `main` 之后跑：

```yaml
# The second tier, added in chapter 9.
#
# Chapter -1 put a guard on the blocking suite -- at most six steps, "the
# blocking suite is meant to stay fast" -- and it is what stopped the step
# below from being added to ci.yml. That guard was written to prevent exactly
# this: a CI file that grows one justified step at a time until nobody waits
# for it. Raising the number to seven would have been the easy answer and the
# wrong one, because the honest reason this check exists is not "it must pass
# before merge".
#
# What it does: applies fourteen one-line mutations to chapter 9's code and
# fails if the test suite does not notice. With all 43 of that chapter's tests
# green, replacing `stderr=PIPE` with `stderr=DEVNULL` in mcp.py turned
# nothing red -- the test whose name says it covers that case asserted the
# process exit code, which comes from `returncode` and not from stderr at all.
# That shape (a test whose name claims more than its assertions) has now
# appeared in chapters 5, 6 and 9.
#
# Why it does not block a merge: it measures the *quality of the tests*, not
# the correctness of the change. A pull request that leaves the code correct
# and a mutation uncaught should be reported and looked at, not held. It has
# no `pull_request:` trigger for that reason, which is asserted in
# tests/test_packaging.py rather than left as a comment.
name: postmerge

on:
  push:
    branches: [main]
  workflow_dispatch: {}

jobs:
  mutation:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true
      - run: uv sync --frozen --all-extras
      - name: Mutation check (chapter 9)
        run: uv run python probe_mutations_ch09.py
```

"它不会挡住合并"是关于它的触发条件的一个事实，所以写成测试，加在 `tests/test_packaging.py` 里：

```python
def test_F_1_05_the_second_tier_cannot_block_a_merge(repo_root: Path) -> None:
    """Chapter 9 added a second workflow rather than a seventh step.

    The six-step cap above is what forced the choice, and it was the right
    forcing function: the mutation check measures the quality of the tests,
    not the correctness of a change, so a pull request that leaves a mutation
    uncaught should be reported and looked at rather than held.

    "It does not block a merge" is a fact about its triggers, so that is what
    is asserted -- a comment saying so would survive somebody adding
    `pull_request: {}` to make it run earlier.
    """
    wf = yaml.safe_load((repo_root / ".github/workflows/postmerge.yml").read_text(encoding="utf-8"))
    triggers = wf.get("on", wf.get(True))
    assert "pull_request" not in triggers
    assert "push" in triggers
    names = [s.get("name", "") for s in wf["jobs"]["mutation"]["steps"]]
    assert any("Mutation" in n for n in names)
```

> 读那个文件，断言触发条件里**没有** `pull_request`、有 `push`。`wf.get("on", wf.get(True))`：YAML 会把没加引号的 `on` 读成布尔值 `True`，两种都试一下。
> 一句写着"这个流程不挡合并"的注释，在有人为了让它早点跑而加上 `pull_request: {}` 之后，仍然会留在那里。

```bash
git add probe_mcp.py probe_mutations_ch09.py .github/workflows/postmerge.yml tests/test_packaging.py
git commit -m "test(mcp): the probe behind every number, and a mutation check that cannot block a merge"
```

---

## §19 收工

### 19.1 这一章动了哪些文件

| 文件 | 新增还是改动 | 在哪一节 |
|---|---|---|
| `pyproject.toml`、`uv.lock` | 加一个依赖 | §3 |
| `.gitignore` | 加两行 | §3 |
| `mcp_servers/files_server.py`、`mcp_servers/notes_server.py` | 新增 | §3 |
| `src/minicodex/mcp.py` | 新增 | §4、§10、§11、§15 |
| `src/minicodex/registry.py` | 新增 | §5 – §14、§16 |
| `src/minicodex/__main__.py` | 改几处 | §16 |
| `mcp.example.json` | 新增 | §16 |
| `tests/test_faults_ch09.py` | 新增 | 分散在各节 |
| `tests/test_packaging.py` | 加一个测试 | §18 |
| `probe_mcp.py` | 新增 | §17 |
| `probe_mutations_ch09.py`、`.github/workflows/postmerge.yml` | 新增 | §18 |

### 19.2 推送、PR

```bash
git push -u origin feat/mcp
```

PR 描述里要如实写的：

> - F09-02、F09-09 **没有复现**。两级加载是按 token 预算触发的；量出来它让选对工具的比例从 3/3 掉到 0/3，这一点没有解决。
> - **第 5 章的沙箱模式管不到别的进程。** 一个 `read-only` 的会话，加上一个能写文件的 MCP server，就是一个能写文件的会话。这一章没有处理。
> - server 悄悄退出时，拿不到原因也拿不到退出码（§10.2），写成了测试。
> - 重启后新出现的工具，这次会话里不提供（§11.2）。
> - server 反过来提问的机制在当前协议版本下送不到（§13.1）；留着处理它的代码，测的是"失败得快、说了原因"。
> - 真模型的测量只有一个服务商、一个模型（gpt-4o-mini）、每组 3 次。

### 19.3 自己审一遍

**1 · `tool_search` 量出来是负作用，为什么还留着？**
因为另一边不是"效果更好"，而是"请求发不出去"：工具的说明超过窗口时，没有别的选择。它是按预算触发的不得已，默认的四千个 token 以内根本不会出现。

**2 · 信 `readOnlyHint`，会不会被一个撒谎的 server 利用？**
会被骗到的只有一件事：同一个 server 上的两个调用被排到一起跑。它换不来和本地工具并行，也换不来绕过审批。

**3 · "一次调用失败了就当它死了"，会不会把一个只是慢的 server 重启掉？**
会：一次超时之后，下一次调用会重启它。重启一个其实还活着的 server，代价是它内存里的状态；不重启一个其实已经死了的，代价是这次会话里它的工具全部没用。选了前一种。

**4 · 重启时要是 server 又起不来呢？**
它的工具全部进 `retired`，以后每次调用都得到"已经停了，而且重启不了"——不会每次调用都去重启一遍，因为登记已经不在了（`_handler` 的第一步）。

**5 · 这一章的测试全都启动真的子进程，慢不慢？**
这一个文件 47 个测试，在 Windows 上二十几秒。SDK 提供一种不起子进程的连接方式，快得多，但这一章有一半的故障需要一个**能死掉**的进程——启动时卡住、调用中退出、临死前写一行。
那种方式下 server 就在测试进程里，它死不了。

---

## §20 codex 是怎么做的

- **它也不自己写协议**：依赖官方的 Rust SDK，而且把版本钉死在一个确切的版本上。
- **名字的拼法一模一样**：`mcp__server__tool`，不合法的字符换成下划线，**原名完整保留**——发给 server 的还是原名。它有一条测试专门钉这件事。
- **它也有 `tool_search`**，默认返回 8 个（这一章是 5 个）。要不要把工具藏到它后面，在 codex 里由一个开关决定；另外有一个按**字节**算的预算，超过的工具对模型不可见。
  后一半和这一章是同一个想法：**按体积管，不按数量管**。前一半这一章没有对应的东西——§7 量出来的结论是，这件事不该有开关，该有的是预算。
- **"注册了、能调用、但模型看不见"在它那里是一个明确的档位**。这一章的 `handlers()` 把藏起来的工具也返回，理由相同：藏的是说明，不是能不能调用。
- **它也把索引放在 `tool_search` 的说明里**，但放的是每个 server 一段，不是每个工具一行。
- **它每一轮重新构造工具列表**，而不是像这一章这样共用一个列表对象。那是更稳妥的设计；这一章没有那样做，是因为前面九章的测试里有几十个手写的假模型客户端，都得跟着改。
- **图片和音频**：它按"这个模型支不支持这种输入"决定保留还是换成一句说明；这一章无条件换成说明。
- **`readOnlyHint` 它也信，而且信得更多**：自称只读的工具直接允许并行。这一章只允许"同一个 server 上的只读调用之间"并行——处理方式最接近、结论更保守的一处。
- **启动的期限是 30 秒**，这一章的默认值是照它抄的。它还会在后台预先启动 server，这一章没有做。

---

## §21 回头看：这一章撞到了什么

**预测到了，并且成立的：** F09-01（但不是崩溃，是悄悄答错）、F09-03（数字小于预测，结论成立）、F09-05。
**预测到了，动工前就挡住的：** F09-04、F09-07、F09-08。
**没有复现的：** F09-02、F09-09。**预测的机制已经不存在的：** F09-06。

**没预测到的：**

| 故障 | 怎么发现的 | 挡住它的东西 |
|---|---|---|
| 清单给 F09-02 开的药（把工具藏起来）是负作用：3/3 变 0/3 | 🟠 量了才信 | 按 token 预算触发，全给或全藏 |
| 前两轮"复现"是探针自己造出来的 | 🟠 把搜到的东西打印出来看 | 第三份目录：六十个各不相同的工具 |
| 列表交出去的是拷贝，机制静默失效 | 🟣 审查 | 两个用 `is` 断言的测试 |
| SDK 把你给的环境变量和它的默认列表合并 | 🟠 问子进程它看见了什么 | 把不想给的置空 |
| 同一句话渲染两遍（文字 + 结构化内容） | 🟠 看实际返回 | `_echoes` |
| 悄悄退出的 server 说不出原因 | 🟢 | **没修**，写成测试 |
| 协议不再允许 server 向客户端提问 | 🔴 调用直接报错 | 断言"失败得快、说了原因" |
| 第七步撞上第 -1 章的六步上限 | 🟢 那个测试红了 | 第二个流程，并断言它不挡合并 |
| **这一章的快照里留着一条变异：整个环境交给每个 server** | 🟢 改写时重跑这个快照的测试，红的 | 改回来；变异只在临时拷贝里跑 |
| **连两个 server 时重启一个，Agent 被"取消"** | 🟣 改写时把示例配置里的两个都连上 | 每条连接一个任务 |
| **重启后新出现的工具：看得见，调不了** | 🟣 改写时问"重启后多了一个会怎样" | 重启只减不加；展示方式不翻转 |
| 两条变异的原文对不上了，脚本报 `could not apply` | ⚪ 变异测试 | 对上；"找不到"一直算作"没抓到" |
| 命令行里 `tools=registry.visible` 那一行没人守 | 🟣 第 8 章学到的那一问 | 一个真的调用 `main()` 的测试 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 审查 · ⚪ 工具

---

## 如果你只记住三件事

1. **别人的工具带来的第一个问题不是崩溃，是一个格式正确、语气笃定的错答案。**
   两个 `search` 进了同一个字典，问文件，得到笔记 server 的"没有匹配"。别人的名字、别人的返回形状、别人的"我是只读的"，都要过一道你自己的手：
   加命名空间，整理成一个字符串，只信到一个结论为止。

2. **一条故障"复现"了的时候，先问复现的是它，还是你的探针；一个办法"省了"的时候，再量一下它别的方面。**
   F09-02 用三份目录撞了三次，前两次都给出了想要的答案。把工具藏起来省了 83% 的 token，同时让选对的比例从 3/3 掉到 0/3。

3. **测试用的配置比真实的配置简单，简单掉的那一部分就是没被测到的那一部分。**
   每个重启测试只连一个 server，示例配置连两个；重启只测过"工具变少"，没测过"工具变多"；这个快照的测试是红的，而没有人跑过它。
   这一章后来发现的三个问题，都是把"真的会怎么用"原样做一遍时出现的。

---

## 动手练习

1. 把 `registry.py` 里 `model_name()` 的返回改成 `return sanitize(tool)`，跑 `uv run pytest tests/test_faults_ch09.py`。红了多少个？
   挑一个看起来和命名毫无关系的，弄明白它为什么也红了。改回去。
2. 写一个 `mcp.json`，把 `files` 这个 server 用两个不同的名字各配一遍。跑 `minicodex ask ... --mcp mcp.json`，启动时那几行说了什么？模型看到的工具名是哪些？
3. 把 `McpRegistry(local=list(TOOL_SCHEMAS))` 改成 `McpRegistry(local=list(TOOL_SCHEMAS), schema_budget=300)`，再跑 §16.3 的那个问题。
   启动时多了哪一行？模型这次用了几轮、搜的是什么词？和 §7.3 的测量对得上吗？
4. `notes_server.py` 的 `search` 是原样匹配，所以 `tools` 找不到 `tool`。**不改 server**，只改它的工具说明能不能让模型换一个词再搜一次？
   （工具说明在 server 那边——想一想，如果这个 server 不是你写的，你能改的是什么。）
5. §19.2 里没处理的那一点：`read-only` 的会话里，一个没有 `readOnlyHint` 的远程工具该不该先问用户？
   先别写代码，先回答：第 5 章的门是按什么判断风险的？一个远程工具，我们知道它的什么、不知道它的什么？

下一章：让 Agent 把一部分工作交给另一个 Agent。到那时"谁能调用什么工具""谁来回答审批的问题"会各多出一层。
