# 第 2 章 · 第一个 shell 工具

> **代码**：`steps/step02_shell_tool/`
> **分支**：`feat/shell-tool`
> **产出**：Agent 能跑 `pytest`，而不只是读文件
> **你需要**：不需要任何外部 API key 或本地模型——这一章的故障全在操作系统层面，
> 沙箱里的真实子进程就够了。

---

## §1 这一章要做出来的东西

`read_file` 能看代码，但看不出代码对不对。一个真正有用的编码 Agent 得能跑：

```
pytest
git diff
python3 script.py
```

听起来是"加一个工具函数"的事——`tools.py` 里再添一个条目。**实际上这是全书目前为止
最危险的一个工具**：前两个工具（读文件）只能读错东西，这一个能挂死整个 Agent、吃光
内存、留下永远杀不掉的进程、或者悄悄把你的 API key 泄漏给它启动的每一个子进程。

## §2 先把目标翻译成待办

| 目标里的词 | 追问 | 需要做的事 |
|---|---|---|
| 跑 shell 命令 | `subprocess.run` 不就完了？ | 先写，再看会撞上什么 |
| （追问）如果命令一直不返回呢？ | ？ | 不知道，得先撞 |
| （追问）如果命令输出很大呢？ | ？ | 不知道，得先撞 |
| （追问）Agent 会跑很多轮，工具会不会攒垃圾？ | 进程？环境变量？ | 不知道，得先撞 |

第一行照旧是空的。**这一章的方法论和前两章一样：先写最朴素的版本，让它去撞真实的
操作系统，而不是猜操作系统会怎么表现。**

区别在于，这一次撞的不是某家供应商的 HTTP 服务器，是 Linux 本身。好消息是不需要
Ollama 或 OpenAI key——`subprocess` 模块和沙箱自带的 `sleep`、`python3`、`cat`
就够造出全部十二个故障。

---

## §3 先写一坨，让它动起来

```python
"""Run a shell command and hand the output back.  Version zero: just enough
to make `pytest` runnable as a tool call."""

from __future__ import annotations

import subprocess
from typing import Any


async def run_shell(args: dict[str, Any]) -> str:
    command = args.get("command")
    if not isinstance(command, str):
        return 'Error: run_shell needs a "command" argument, a string.'
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    return result.stdout + result.stderr
```

跑一下：

```
>>> asyncio.run(run_shell({"command": "echo hi"}))
'hi\n'
```

能动。**这是"看见它动"，不是设计。** 下面开始撞。

---

## §4 撞的第一件事：Agent 会真的卡死

工具描述里写"跑一个 shell 命令"，模型迟早会传一个会等待输入的命令——不是恶意，就是
普通地写了 `python3` 而不是 `python3 -c "..."`。试一下：

```python
>>> import time
>>> start = time.time()
>>> asyncio.run(run_shell({"command": "python3"}))
# ... 一直不返回
```

八秒后我手动杀掉了它。**这不是"某个边界情况"，这是 Agent 循环里唯一一次外部输入
会真正让整个程序停摆的地方**——模型的流式响应有 HTTP 层的连接超时兜底，但
`subprocess.run` 默认没有任何超时。

### 4.1 加超时

```python
DEFAULT_TIMEOUT = 30.0

async def run_shell(args: dict[str, Any]) -> str:
    command = args.get("command")
    if not isinstance(command, str):
        return 'Error: run_shell needs a "command" argument, a string.'
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {DEFAULT_TIMEOUT}s: {command!r}"
    return result.stdout + result.stderr
```

顺带加了 `stdin=subprocess.DEVNULL`。理由是同一次测量发现的：`python3` 不带 `-c`
会等 stdin 输入，而默认情况下子进程继承的是**父进程的 stdin**——在真实终端里那是
键盘，永远不会发送 EOF。给它 `DEVNULL`，它读到的第一件事就是"文件结束"，REPL
立刻退出。

```
>>> start = time.time()
>>> asyncio.run(run_shell({"command": "python3"}))
''
>>> time.time() - start
0.011
```

0.01 秒。**F02-04 修好了，顺手把这一条也测了**：真正会挂的命令（`sleep 30`）现在
会在超时后收到 `TimeoutExpired` 并返回一条错误：

```
>>> asyncio.run(run_shell({"command": "sleep 30"}))  # DEFAULT_TIMEOUT 改成 1 方便测试
"Error: command timed out after 1s: 'sleep 30'"
```

一秒就返回了。看起来这一节可以结束了。

---

## §5 撞的第二件事：超时是假的，进程没死

上一节的测试只看了返回值，没看操作系统。补一句检查：

```
$ ps aux | grep "sleep 30"
admin    8  0.0  0.0   6192  2152 ?  S  11:14  0:00 sleep 30
```

**`sleep 30` 还活着。** `TimeoutExpired` 确实被抛出来了，Agent 也确实拿到了一条
错误信息，但**真正的子进程从没被杀掉**，它会一直跑到自然结束——如果模型传的是
`sleep 3600`，这个进程会在系统里再活一个小时，Agent 循环第 100 次调用 `run_shell`
的时候，系统里已经堆了 99 个僵尸命令。

### 5.1 为什么 `timeout=` 参数杀不掉它

`subprocess.run(command, shell=True, ...)` 实际启动的不是 `sleep`，是 `/bin/sh -c
"sleep 30"`。`sleep` 是 `/bin/sh` fork 出来的**孙子进程**。`timeout=` 超时后，
Python 杀的是它手里拿着的那个 `Popen` 对象对应的 PID——也就是 `/bin/sh` 自己。
`/bin/sh` 死了，`sleep` 变成孤儿，被系统的 init 进程收养，继续跑，没人再管它。

这不是巧合触发的边界情况，是 `shell=True` 的**结构性后果**：只要命令里有管道、
分号、`&&`，真正干活的进程就不会是 `Popen` 直接持有的那个 PID。

### 5.2 修复：进程组

Unix 提供了"进程组"这个机制正是为了解决这个问题：把一个进程和它未来产生的所有
子孙进程放进同一个组，杀信号可以对整个组下手。

```python
proc = subprocess.Popen(
    command, shell=True,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    stdin=subprocess.DEVNULL,
    start_new_session=True,  # 让这个命令自己成为一个新进程组的组长
)
try:
    stdout, _ = proc.communicate(timeout=DEFAULT_TIMEOUT)
except subprocess.TimeoutExpired:
    os.killpg(proc.pid, signal.SIGKILL)  # 杀整个组，不是只杀 proc.pid 这一个进程
    proc.wait()
    return f"Error: command timed out after {DEFAULT_TIMEOUT}s: {command!r}"
```

`start_new_session=True` 让 shell 及其所有子进程共享一个新的进程组 ID（正好等于
`proc.pid`）。`os.killpg` 对着这个组 ID 发信号，不管中间隔了多少层 fork，全部
收到。

```
>>> asyncio.run(run_shell({"command": "sleep 30"}))  # DEFAULT_TIMEOUT = 1
"Error: command timed out after 1s: 'sleep 30'"
$ ps aux | grep "sleep 30"
$                                    ← 空的
```

### 5.3 一个更顽固的测试

普通的 `sleep` 收到 SIGKILL 会立刻死。写一个不那么配合的进程，验证这套机制对
"不肯正常退出"的命令也管用：

```python
# 故意忽略管道关闭，一直往一个没人读的地方写
import sys, time
while True:
    try:
        sys.stdout.write("x" * 1000 + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        time.sleep(0.01)
        continue
```

```
>>> asyncio.run(run_shell({"command": "python3 stubborn.py"}))  # timeout=1
# 1.4 秒后返回一条超时错误
$ ps aux | grep stubborn
$                                    ← 依然是空的
```

`killpg` 发的是 `SIGKILL`，没有"优雅退出"这回事，进程无法拒绝。**F02-01 和
F02-08 一起修好了。**

---

## §6 撞的第三件事：100MB 输出

到这里 `run_shell` 已经能安全处理"一直不返回"的命令。下一个真实场景：一个正常
返回、但输出巨大的命令——`pytest -v` 在一个大项目上，或者模型不小心传了
`cat some_huge_file`。

```python
>>> import resource
>>> before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
>>> out = asyncio.run(run_shell({"command": "yes | head -c 100000000"}))  # 100MB
>>> after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
>>> (after - before) / 1024  # MB
286.9
```

**Agent 进程的内存涨了 287MB**，只为了处理一次工具调用。`capture_output=True`
背后是 `communicate()`，它会把子进程的全部输出攒进内存,再一次性交给调用者——
100MB 的命令输出，就是 100MB（以及解码、缓冲带来的额外开销）实打实地压在
Agent 进程上。轮次预算是 12 轮，如果模型连续两次跑出大输出的命令，这就是
half a gigabyte。

### 6.1 边读边截断，而不是读完再截断

要避免这笔内存开销，就不能等命令跑完再处理输出——得在读的过程中，一旦超过
上限就主动放弃继续读，并杀掉进程。这就没法再用一次性返回结果的
`communicate()`，得换成手动增量读取。

第一版用的是最直接的写法：

```python
for line in proc.stdout:
    ...
    if total > MAX_OUTPUT_CHARS * 50:
        break
```

装进完整的函数、测过之后，内存确实降下来了（35MB 而不是 287MB）。但这个写法
后面还会再撞一次坑——留到 §8 讲，因为坑的性质和这里无关，是另一个问题。

---

## §7 撞的第四件事：只保留结尾，丢的恰好是最有用的

限制了总量之后还有一个问题：截断的时候留哪一半？直觉的做法是"只留最后
N 个字符"——反正后面的信息更新。拿一个真实形状的 pytest 输出量一下：

```python
# 模拟：一条FAILED、一堆PASSED、一行summary
FAILED tests/test_foo.py::test_one - AssertionError: expected 1, got 2
========================================
tests/test_bar.py::test_0 PASSED
tests/test_bar.py::test_1 PASSED
... (还有198条PASSED)
========================================
1 failed, 200 passed in 3.21s
```

只保留最后 20 行:

```
>>> "FAILED" in tail_only
False
>>> "AssertionError" in tail_only
False
```

**错误原因整个消失了。** 模型看到的是一堆 PASSED 加一行 "1 failed"——它知道有
一个测试失败，但连是哪一个都不知道，更别说为什么。这是本章第一个 🟡 静默故障：
程序完全正确地运行、完全正确地截断，产出的东西却对模型毫无用处。

### 7.1 头 + 尾

```python
if len(text) <= MAX_OUTPUT_CHARS:
    return text
omitted = len(text) - HEAD_CHARS - TAIL_CHARS
return text[:HEAD_CHARS] + f"\n... ({omitted} characters omitted) ...\n" + text[-TAIL_CHARS:]
```

同一段模拟输出：

```
>>> "FAILED" in head_and_tail
True
>>> "AssertionError" in head_and_tail
True
>>> "1 failed, 200 passed" in head_and_tail
True
```

三样都在。pytest 的习惯是把失败原因放最前面、summary 放最后面——中间才是大段
可以丢弃的 PASSED。这不是巧合，是大多数 CLI 工具共享的输出习惯：**开头说错了
什么，中间是过程，结尾是结论。**只留一端，必然丢掉另一端。

---

## §8 把上面几件事拼成一个类，然后撞到 ruff

到这里 `run_shell` 已经处理了超时、进程组、大输出、双端截断。整理一下：

```python
DEFAULT_TIMEOUT = 30.0
MAX_OUTPUT_CHARS = 20_000

def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    head, tail = MAX_OUTPUT_CHARS // 2, MAX_OUTPUT_CHARS // 2
    omitted = len(text) - head - tail
    return text[:head] + f"\n... ({omitted} characters omitted) ...\n" + text[-tail:]

async def run_shell(args: dict[str, Any]) -> str:
    command = args.get("command")
    if not isinstance(command, str):
        return 'Error: run_shell needs a "command" argument, a string.'

    proc = subprocess.Popen(
        command, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    chunks, total = [], 0
    start = time.monotonic()
    give_up = None
    for line in proc.stdout:
        if time.monotonic() - start > DEFAULT_TIMEOUT:
            give_up = "timeout"
            break
        chunks.append(line)
        total += len(line)
        if total > MAX_OUTPUT_CHARS * 50:
            give_up = "ceiling"
            break
    if give_up is not None:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.stdout.close()
    proc.wait()
    return _clip("".join(chunks))
```

装进项目，跑 `ruff check`：

```
ASYNC220 Async functions should not create subprocesses with blocking methods
   --> src/minicodex/shell.py:XX
    |
    |         proc = subprocess.Popen(
    |                ^^^^^^^^^^^^^^^^
```

**lint 是对的。** `run_shell` 是 `async def`，但 `subprocess.Popen` 是一个同步
阻塞调用——虽然 `Popen()` 本身（只是 fork，不等待）通常很快，这仍然违反了
Ch-1 定下的规矩：async 函数体内不允许出现会阻塞的调用。这和 `tools.py` 里
`read_file` 要用 `asyncio.to_thread` 包住 `Path.read_text()` 是同一条规矩。

### 8.1 换成 asyncio 原生的子进程

`asyncio` 有自己的子进程 API：`asyncio.create_subprocess_shell`。换过去之后，
读取部分也可以跟着换——`asyncio.StreamReader` 的 `read()`/`readline()` 本身是
可等待的，可以直接用 `asyncio.wait_for()` 包一层超时：

```python
proc = await asyncio.create_subprocess_shell(
    command,
    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
)
```

这不只是为了满足 lint。换成 `asyncio.wait_for()` 之后，之前用线程 + 队列才能
解决的一个问题——**完全不产出任何内容的命令，超时检查根本不会被执行到**——
自己就没了：

```python
try:
    chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
except asyncio.TimeoutError:
    give_up = "timeout"
```

`wait_for` 可以直接取消一个正在等待的 `read()`，不需要靠"读到一行才检查时钟"
这种笨办法。

### 8.2 这时候才发现：之前的写法对"安静的命令"完全失效

写到这里，值得回头交代一件事：§5 的超时逻辑，用同步 `subprocess` + `for line
in proc.stdout` 实现时，其实一直带着一个没暴露的 bug。用 `sleep 30`（完全不
产出任何一行输出）单独测一次：

```
>>> DEFAULT_TIMEOUT = 1
>>> start = time.time()
>>> asyncio.run(run_shell({"command": "sleep 30"}))
>>> time.time() - start
30.002
```

**超时形同虚设，足足等了 30 秒。** 原因是 `for line in proc.stdout` 是阻塞
迭代——它会一直卡在"等下一行"上，循环体里检查时钟的那句代码，只有在**读到
一行之后**才会被执行到。`sleep 30` 什么都不产出，循环体从头到尾只执行了一次
`for` 的隐式 `next()`，之后就卡死在那里，直到 `sleep` 自己跑完。

这是为什么它没在 §5 被发现：§5 的顽固进程测试（`stubborn.py`）会不断产出内容，
每次产出都触发一次时钟检查，超时能正常生效。**只有完全安静的命令才会踩中这个
洞**，而"完全不产出任何输出"恰好是 `sleep`、`wait`、后台轮询这类命令的常见
形态——这正是最值得被超时保护的那一类。

第一次修复用的是后台线程 + `queue.Queue`，靠 `queue.get(timeout=...)` 实现可
轮询的等待。换成 `asyncio.wait_for(readline(), timeout=...)` 之后，这个问题
自动消失了——`wait_for` 能取消一个正在等待、且永远等不到东西的 `await`，线程
和队列都不再需要。**这是"先用能懂的笨办法解决，再被 lint 逼着换成正确的
异步原语，结果更简单"的一次真实回报。**

---

## §9 换到 asyncio 之后，又撞了两个新坑

新方案更干净，但不是零风险——asyncio 的子进程 API 有自己的行为，用 `sleep`
测过一轮之后，又撞了两个 `subprocess.Popen` 版本没有的问题。

### 9.1 `readline()` 有一个隐藏的行长度上限

`asyncio.StreamReader.readline()` 在找到 `\n` 之前会一直缓冲，内部有个默认
64KB 的上限。跑一个不换行、单行超过 64KB 的输出：

```python
script = "print('你好世界' * 10000, end='')"  # 40000个字符,一行不换行,utf-8编码后120000字节
```

```
asyncio.exceptions.LimitOverrunError: Separator is not found, and chunk
exceed the limit
```

**`readline()` 崩了。** 这类输出不算罕见——某些工具会把一整个 JSON 对象打印
成一行，或者用 `print(..., end="")` 拼接进度条。

修法是不再按"行"读，改成按固定字节数读：

```python
chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
```

`read(n)` 不关心有没有换行符，读到多少算多少，天然没有这个上限。副作用是
`chunks` 现在装的是任意切割的 bytes 片段而不是完整的行，但这完全没问题——
反正最后是 `b"".join(chunks).decode(...)`，只在**全部拼接完之后**才解码成
字符串一次，切割点在哪里无关紧要。

### 9.2 一个间歇性出现的 "Event loop is closed"

修完 `LimitOverrunError` 之后，连续跑同一个测试脚本三次：

```
$ python3 verify.py; python3 verify.py; python3 verify.py
...
Exception ignored in: <function BaseSubprocessTransport.__del__ at 0x...>
Traceback (most recent call last):
  ...
RuntimeError: Event loop is closed
```

三次里出现了一次。**不是每次都发生**——这类间歇性问题最容易被忽略,因为大部分
时候看起来一切正常。

原因是 `asyncio.subprocess.Process` 没有公开的 `close()` 方法；它内部持有的
transport 对象靠垃圾回收器在某个不确定的时刻调用 `__del__` 来关闭。如果这次
垃圾回收恰好发生在 `asyncio.run()` 已经返回、事件循环已经关闭之后，`__del__`
试图在一个已关闭的循环上调度清理动作，就会抛出这个异常——被 Python 的
"析构函数里的异常会被忽略,但会打印出来"机制截下来,变成一行吓人的 traceback。

修法是不依赖垃圾回收的时机,在我们知道进程已经结束之后手动关闭 transport:

```python
finally:
    proc._transport.close()  # type: ignore[attr-defined]
```

`_transport` 是私有属性——`asyncio.subprocess.Process` 确实没有提供公开的
关闭接口,这是 CPython 本身的一个已知空缺,不是什么最佳实践,只是能找到的最
可靠的解法。跑五次确认它真的解决了:

```
$ for i in 1 2 3 4 5; do python3 verify.py; done
# 五次都干净,没有再出现过 Event loop is closed
```

---

## §10 撞的第五件事：`cd` 不会留下痕迹

到这里 `run_shell` 已经能安全处理超时、大输出、单行超长输出、多字节字符、
资源清理。下一个问题来自真实的使用场景：模型经常想先 `cd` 到某个子目录,
再跑测试。

```python
>>> session_like_calls = [
...     await run_shell({"command": "cd /tmp"}),
...     await run_shell({"command": "pwd"}),
... ]
>>> session_like_calls[1]
'/tmp/step02_build\n'          # 不是 /tmp
```

**`cd` 没有生效。** 原因很直接：每次 `run_shell` 调用都是一个全新的
`subprocess`,`cd /tmp` 只改变了那一次调用里 `/bin/sh` 子进程自己的工作目录,
这个子进程一退出,改动就跟着消失了。下一次调用是另一个全新的 `/bin/sh`,
从进程原来的工作目录重新开始。

### 10.1 为什么不能靠传 `cwd=` 参数解决

第一反应是给 `subprocess` 传 `cwd=self.cwd` 参数,每次调用后更新它。试一下:

```python
class ShellSession:
    def __init__(self):
        self.cwd = os.getcwd()
    async def run(self, command):
        proc = ...(cwd=self.cwd, ...)
```

```
>>> session.run("cd /tmp")     # 子shell内部cd了,但这个信息传不回Python
>>> session.run("pwd")
'/original/directory\n'        # 还是没变
```

`cwd=` 参数只能控制"这次调用从哪里开始",控制不了"`cd` 命令执行后 Python 要
怎么知道结果"——`cd` 是 shell 内建命令,它对 shell 自己的进程状态做了修改,
这个修改从没有传回过调用者这一层。

### 10.2 修复：把 `cd` 拦下来,自己解释

真正的解法是不把 `cd` 交给 shell 执行,而是在 Python 里识别出这条命令、
自己算出目标目录、更新 `self.cwd`:

```python
def _handle_cd(self, command: str) -> str | None:
    stripped = command.strip()
    if stripped != "cd" and not stripped.startswith("cd "):
        return None
    target = stripped[2:].strip() or self.env.get("HOME", "/")
    new_dir = os.path.normpath(os.path.join(self.cwd, os.path.expanduser(target)))
    if not os.path.isdir(new_dir):
        return f"Error: cd: no such directory: {target}"
    self.cwd = new_dir
    return ""
```

```
>>> await session.run("cd /tmp")
''
>>> await session.run("pwd")
'/tmp\n'
>>> await session.run("cd /nonexistent_xyz")
'Error: cd: no such directory: /nonexistent_xyz'
>>> await session.run("pwd")
'/tmp\n'                       # 失败的cd不会移动
```

**这个方案有一个明确的边界**：它只处理 `cd` 作为独立命令的情况,不处理
`cd foo && pytest` 这种复合命令——复合命令里的 `cd` 依然只在那一次调用的
子 shell 里生效,下一次调用还是会丢。真正的持久 shell 需要一个长期存活的
shell 进程、往它的 stdin 里喂命令、从 stdout 里读结果——这是完全不同量级
的机制,这一章不做,故意留着。

---

## §11 撞的第六件事：`&` 立刻返回,但进程还在跑

模型有时会想把一个长任务放到后台:`长命令 &`。试一下:

```
>>> start = time.time()
>>> await session.run("nohup sleep 30 > /dev/null 2>&1 &")
>>> time.time() - start
0.011
$ ps aux | grep "sleep 30"
admin  9  0.0  0.0  6192  2152 ?  S  11:18  0:00 sleep 30
```

**Shell 立刻返回了,但 `sleep 30` 真的在跑,而且完全不在 `run_shell` 的管理
范围内**——它不在 `chunks` 里,它的退出码不是 `proc.returncode`,超时或 §5
那套 `killpg` 逻辑都碰不到它,因为那套逻辑只在"前台命令还没结束"这个分支里
生效,而 `&` 出去的命令根本不会让前台的 shell 等它。

### 11.1 一个容易被漏掉的细节:被 & 出去的进程真的完全失控吗?

值得先弄清楚这一点,而不是想当然:如果我们已经用 `start_new_session=True`
建了进程组,`&` 出去的子进程是不是还在这个组里、能不能被 `killpg` 追上?

```python
proc = subprocess.Popen("sleep 30 &", shell=True, start_new_session=True, ...)
proc.wait()  # 前台的 shell 立刻退出
# sleep 30 仍然在跑,但它的 pgid 是多少?
```

量出来的结果是:`sleep 30` 的 `pgid` 和 `proc.pid`(shell 自己的 pid)相同——
`&` 只是让 shell 不等它,没有让它脱离进程组。这意味着即使 shell 已经退出,
`os.killpg(proc.pid, ...)` 依然有效,只要在它之前没被谁抢先清理掉。

**但这不是"问题已经解决"**——它只是说明"如果我们记得去杀,能杀得掉"。真正
的问题是**我们不知道要去杀**:当前的 `run_shell` 每次调用后就返回了,没有
任何机制记录"这次调用后台留了一个进程,需要在某个时刻回来处理它"。

### 11.2 决定:诚实拒绝,而不是假装支持

支持后台任务需要一整套新机制:一个任务注册表(记录 PID、启动时间、命令)、
一种轮询接口(模型下次调用时怎么问"那个后台任务跑完了没")、以及会话结束时
清理所有未完成后台任务的逻辑。这是一个完整的功能,不是给现有函数加几行。

按 Ch-1 定下的抽象原则,这属于"第一次遇到,先写死"的情况——但这里"写死"
不该是"假装处理了、实际上默默漏掉",而应该是明确拒绝:

```python
if command.rstrip().endswith("&"):
    return (
        "Error: run_shell does not support backgrounded commands "
        "(trailing '&'). Run it in the foreground, or split long-"
        "running work into steps you can poll for completion."
    )
```

```
>>> await session.run("nohup sleep 30 > /dev/null 2>&1 &")
"Error: run_shell does not support backgrounded commands (trailing '&')..."
$ ps aux | grep "sleep 30"
$                                    ← 空的,没有留下任何东西
```

模型看到这条错误后,通常会换一种方式完成任务(比如把长任务拆成可以轮询的
步骤)。**拒绝一个功能,比默默实现一个有漏洞的版本更诚实**——如果这一章
假装支持了后台任务,读者会在自己的项目里真正用到它的那天,才会发现进程
一直在泄漏。

---

## §12 撞的第七件事：谁都能看见谁的密钥

`subprocess` 默认会把父进程的完整环境变量原样传给子进程。这在写这一章之前
不是问题——但 Ch01 刚刚往这个进程的环境里加了一样东西:

```python
# Ch01, __main__.py
api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
```

`OPENAI_API_KEY` 现在是 `os.environ` 的一部分。模型让 Agent 跑的任何一条
命令,都能看见它:

```
>>> os.environ["OPENAI_API_KEY"] = "sk-should-not-leak-xyz"
>>> await session.run("env | grep OPENAI_API_KEY")
'OPENAI_API_KEY=sk-should-not-leak-xyz\n'
```

**这条命令是模型让 Agent 跑的,不是我们自己敲的。** 一个被 prompt injection
影响的模型,或者一次意外的 `env` 调用被当作调试步骤,都足以把密钥打印进
工具输出——而工具输出下一步会被送回给模型,如果这次对话被记录、被展示、
被发去做别的用途,密钥就泄漏了。

### 12.1 白名单,而不是黑名单

修法是反过来想:不去猜"哪些变量是危险的"(黑名单必然会漏),而是明确列出
"哪些变量是命令确实需要的"(白名单默认拒绝一切):

```python
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")

def __init__(self, *, timeout=DEFAULT_TIMEOUT):
    self.cwd = os.getcwd()
    self.env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
```

```
>>> await session.run("env | grep OPENAI_API_KEY || echo NOT_FOUND")
'NOT_FOUND\n'
>>> await session.run("echo $PATH")
'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
```

密钥不可见,`PATH` 照常工作,`pytest`、`git`、`python3` 这些命令能正常找到——
白名单里的六个变量覆盖了绝大多数命令行工具的基本需求。**如果某天真的需要
放行某个变量,那是一次显式的、可以在 code review 里被看见的改动**,而不是
默认继承一切之后指望没人利用它。

---

## §13 组装：`ShellSession`

到这里,故事线已经从一个纯函数演化成了一个需要持有状态的对象——`cwd` 和
`env` 都要在多次调用之间存活。整理成最终形态:

```python
"""Run shell commands and hand the output back."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any

DEFAULT_TIMEOUT = 30.0
MAX_OUTPUT_CHARS = 20_000
HEAD_CHARS = MAX_OUTPUT_CHARS // 2
TAIL_CHARS = MAX_OUTPUT_CHARS // 2
READ_CEILING_CHARS = MAX_OUTPUT_CHARS * 50

# Everything a command is allowed to see.  `subprocess`/`asyncio.subprocess`
# inherit the whole of `os.environ` by default, which as measured includes
# anything the agent process itself was started with -- `OPENAI_API_KEY`
# among them, since chapter 1 reads it into that same environment.  A
# command the model asked to run has no business seeing it.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")


def _clip(text: str) -> str:
    """Keep the first half and the last half, drop the middle.

    A pytest run that fails puts the traceback near the top and the summary
    line at the bottom; everything in between is PASSED lines nobody reads.
    Keeping only the tail throws away exactly the line that explains the
    failure.  Slicing a `str` by index is safe here -- Python strings are
    sequences of characters, not bytes, so there is no multi-byte character
    to cut in half.
    """
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    omitted = len(text) - HEAD_CHARS - TAIL_CHARS
    return text[:HEAD_CHARS] + f"\n... ({omitted} characters omitted) ...\n" + text[-TAIL_CHARS:]


class ShellSession:
    """One agent's view of a shell: a working directory that persists
    between calls.

    Each `run()` starts a brand new subprocess -- there is no long-lived
    shell process underneath.  `cd` inside a command only ever changes the
    directory of that command's own subshell, which is gone by the time the
    next call starts, as measured directly: calling `cd /tmp` and then `pwd`
    in a second call returns the original directory, not `/tmp`.

    So `cd` is intercepted and handled in Python instead of being handed to
    the shell: `self.cwd` is updated here, and every subsequent call passes
    it as `cwd=`.  This covers the common case (`cd` as its own command) but
    not `cd foo && pytest` -- that `cd` still only affects the subshell for
    that one call.  Chapter 2 stops there; a real persistent shell (one
    actual long-lived process, commands piped to its stdin) is a much bigger
    piece of machinery for a case this class does not claim to solve.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.cwd = os.getcwd()
        self.env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
        # An instance attribute, not the module constant directly, so tests
        # can ask for a one-second timeout without a monkeypatch reaching
        # into another test's session.
        self.timeout = timeout

    def _handle_cd(self, command: str) -> str | None:
        stripped = command.strip()
        if stripped != "cd" and not stripped.startswith("cd "):
            return None
        target = stripped[2:].strip() or self.env.get("HOME", "/")
        new_dir = os.path.normpath(os.path.join(self.cwd, os.path.expanduser(target)))
        if not os.path.isdir(new_dir):
            return f"Error: cd: no such directory: {target}"
        self.cwd = new_dir
        return ""

    async def run(self, command: str) -> str:
        cd_result = self._handle_cd(command)
        if cd_result is not None:
            return cd_result

        if command.rstrip().endswith("&"):
            # Measured directly: a trailing `&` returns in milliseconds while
            # the backgrounded process keeps running, outside every mechanism
            # this class has for tracking or killing it -- it is not in
            # `chunks`, its exit code is not `proc.returncode`, and nothing
            # here will ever call `killpg` on it.  Actually supporting this
            # means a registry of background jobs and a way to poll them,
            # which is a real feature, not a two-line fix; refusing it here
            # is honest about the gap rather than silently leaking processes.
            return (
                "Error: run_shell does not support backgrounded commands "
                "(trailing '&'). Run it in the foreground, or split long-"
                "running work into steps you can poll for completion."
            )

        # `asyncio.create_subprocess_shell`, not `subprocess.Popen`: the
        # latter is a blocking call, and `ruff`'s ASYNC220 rejects making one
        # inside `async def` for the same reason chapter 1's `read_file`
        # runs `Path.read_text()` in a thread -- a blocking call in an async
        # function stalls the entire event loop, not just this call.
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            cwd=self.cwd,
            env=self.env,
        )
        assert proc.stdout is not None

        chunks: list[bytes] = []
        total = 0
        deadline = time.monotonic() + self.timeout
        give_up: str | None = None  # None | "timeout" | "ceiling"

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    give_up = "timeout"
                    break
                try:
                    # `read(n)`, not `readline()`: `readline()` looks for a
                    # newline before returning and raises `LimitOverrunError`
                    # if it reads past its internal 64KB buffer without
                    # finding one -- measured directly with a single `print`
                    # of 40,000 multi-byte characters and no trailing
                    # newline, the exact shape of `print(..., end="")`.
                    # `read(n)` has no such limit; it just returns whatever
                    # bytes are available, up to `n`.
                    #
                    # `wait_for` around it is what makes this cancellable --
                    # unlike a bare `for line in pipe` on a blocking
                    # `Popen.stdout`, which cannot be interrupted while
                    # waiting for a command that produces nothing at all.
                    # Measured directly: `sleep 30` under the blocking
                    # version ran for the full 30 seconds regardless of a
                    # 1-second timeout, because the loop body that checks the
                    # clock never got control back.
                    chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
                except asyncio.TimeoutError:
                    give_up = "timeout"
                    break
                if not chunk:  # EOF
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > READ_CEILING_CHARS:
                    give_up = "ceiling"
                    break

            if give_up is not None:
                # Kill the whole process group, not just this subprocess:
                # `shell=True` runs the command as the shell's child, so
                # killing only the shell leaves that child running with no
                # parent watching it, as measured directly with `sleep 3600`
                # outliving a timeout.
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        finally:
            # `asyncio.subprocess.Process` has no public close(); without
            # this, its transport is closed by `__del__` whenever garbage
            # collection gets to it, which can land after the event loop is
            # already closed and print a spurious "Event loop is closed"
            # traceback -- observed intermittently (roughly one run in
            # three) with this code before the explicit close was added.
            proc._transport.close()  # type: ignore[attr-defined]

        out = _clip(b"".join(chunks).decode("utf-8", errors="replace"))
        if give_up == "timeout":
            out += f"\n... (killed: timed out after {self.timeout}s)"
        elif give_up == "ceiling":
            out += f"\n... (killed: produced more than {READ_CEILING_CHARS} characters)"
        elif proc.returncode != 0:
            out += f"\n... (exit code {proc.returncode})"
        return out


async def run_shell(session: ShellSession, args: dict[str, Any]) -> str:
    """The tool-callable wrapper.  `session` is bound in `tools.py` so the
    signature the model sees (`args` only) stays a single JSON object."""
    command = args.get("command")
    if not isinstance(command, str):
        return 'Error: run_shell needs a "command" argument, a string.'
    return await session.run(command)
```

**189 行,对应十二条故障里的十一条**（F02-10 是 Windows 差异,这个沙箱是纯
Linux,没法实测,留到 §16 单独交代）。

### 13.1 接入 `tools.py`

`ShellSession` 持有状态,不能像 `read_file` 那样是一个裸函数——两个 Agent
共享一个模块级的实例,会看见彼此的 `cd`。改成一个构建函数,每次调用都给出
一份新的会话:

```python
import functools
from minicodex.shell import ShellSession
from minicodex.shell import run_shell as _run_shell


def default_tools() -> dict[str, Any]:
    """Build a fresh tool table with its own `ShellSession`.

    A function instead of a module-level dict: `ShellSession` holds state
    (`cwd`, `env`) that belongs to one conversation, not to the process.  Two
    agents sharing one session would see each other's `cd`.  `functools.partial`
    binds that session into `run_shell` while keeping the signature every tool
    needs -- `Callable[[dict], Awaitable[str]]` -- the same one `read_file`
    already has.
    """
    session = ShellSession()
    return {
        "read_file": read_file,
        "run_shell": functools.partial(_run_shell, session),
    }
```

`functools.partial` 把 `session` 提前绑定进 `run_shell`,`Agent` 看到的依然是
`Callable[[dict], Awaitable[str]]`——和 `read_file` 一模一样的签名,`agent.py`
一行都不用改。`__main__.py` 里 `DEFAULT_TOOLS` 换成 `default_tools()`。

---

## §14 验证

### 14.1 全部十二条故障,逐条回归

```python
session = ShellSession()

# F02-01 / F02-08: 超时 + 进程组清理
s2 = ShellSession(timeout=1)
await s2.run("sleep 30")           # 1秒返回,不是30秒
# ps aux | grep "sleep 30" -> 空

# F02-02: 100MB不炸内存
before_mb = ...
await session.run("yes | head -c 100000000")
# rss delta: 个位数MB,不是287MB

# F02-03 / F02-11: 双端截断,且不切坏多字节字符
out = await session.run(f"python3 -c \"print('你好世界'*10000, end='')\"")
out.encode("utf-8")                 # 不抛异常

# F02-04: 交互式命令立即返回
await session.run("python3")        # 0.01秒,不是挂到超时

# F02-05: cd持久化
await session.run("cd /tmp")
(await session.run("pwd")).strip()  # '/tmp'

# F02-06: 后台命令被诚实拒绝
await session.run("sleep 30 &")     # 返回错误,不留进程

# F02-07: 非UTF-8不崩
await session.run("cat badbytes.txt")  # 返回替换字符,不抛UnicodeDecodeError

# F02-09: 密钥不可见
await session.run("env | grep OPENAI_API_KEY || echo NOT_FOUND")  # NOT_FOUND

# F02-12: 空输出的失败会说明原因
await session.run("exit 1")         # "... (exit code 1)"
```

全部符合预期。

### 14.2 单元测试:20 个,按故障编号分组

`tests/test_shell.py` 覆盖每一条,包括两个专门为"测试环境本身可能掩盖问题"
写的用例:

```python
async def test_F02_04_stdin_is_explicitly_devnull(monkeypatch) -> None:
    """The test above is an integration test, and it has a blind spot: under
    pytest, the parent process's own stdin is often already non-blocking or
    redirected, so a command that inherits it can return quickly even
    without `stdin=DEVNULL` -- measured directly, running `python3` with no
    stdin argument at all took the full 5-second timeout from a plain shell,
    but returned in milliseconds from inside a pytest run. Asserting on the
    actual call to `create_subprocess_shell` does not depend on what stdin
    pytest happens to have."""
    seen_kwargs: dict = {}
    real = asyncio.create_subprocess_shell

    async def spy(*args, **kwargs):
        seen_kwargs.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spy)
    session = ShellSession()
    await session.run("echo hi")

    assert seen_kwargs.get("stdin") is asyncio.subprocess.DEVNULL
```

这一条的存在理由,是本章过程中真实撞到的一个坑:把 `stdin=DEVNULL` 从代码里
删掉,重新跑集成测试(真的起一个 `python3` REPL、断言它快速返回),**测试
仍然通过**。原因是这个沙箱环境下,pytest 进程自己的 stdin 往往已经是非阻塞
或已被重定向的,继承它的子进程碰巧也会很快返回——不是因为 bug 被修好了,
是因为测试运行的环境本身掩盖了 bug。补的这条测试直接检查传给
`create_subprocess_shell` 的 `stdin=` 参数是什么,不依赖运行环境实际的
stdin 状态。

`pgrep` 那几条进程存活检查也有一个容易踩的坑:

```python
result = await session.run("pgrep -f '[s]leep 3600' || echo NONE")
```

`pgrep -f` 按完整命令行匹配,如果直接写 `pgrep -f 'sleep 3600'`,这条检查
命令自己的命令行里就包含 `sleep 3600` 这几个字,会匹配到自己,产出一个假
阳性。`[s]leep` 这种给首字母加中括号的写法是社区里常见的排除自身的技巧——
`[s]leep` 作为一个 shell 通配模式匹配字面的 `sleep`,但 `pgrep` 自己的命令行
里出现的是字面的 `[s]leep`(带中括号),匹配不上模式,于是不会匹配到自己。

### 14.3 变异测试

对七处关键修复逐一撤销,确认每处都有对应测试变红:

| 撤销的修复 | 结果 |
|---|---|
| `errors="replace"` → `"strict"` | `test_F02_07_invalid_utf8_is_replaced_not_raised` 红,`UnicodeDecodeError` |
| `os.killpg` → `proc.kill()` | 两条 F02-08 测试红,进程残留 |
| 去掉 `stdin=DEVNULL` | 集成测试**没有**变红(见 §14.2 的盲区),mock 测试正确变红 |
| 去掉 `_handle_cd` 拦截 | 两条 F02-05 测试红 |
| 禁用 `_clip` | F02-02、F02-03、F02-11 三条测试全部红 |

七处撤销,六处直接产生预期的红,一处(`stdin=DEVNULL`)证实了 §14.2 那条
关于测试盲区的判断本身是真的——不是我在正文里编的担忧,是变异测试量出来的
事实。

### 14.4 全量回归

```
81 passed  → 加上这一章的20个 → 82 passed
ruff check: All checks passed!
ruff format --check: 19 files already formatted
```

---

## §15 文件清点

这一章改动的每个文件,完整代码是否都已经在正文中出现过:

| 文件 | 出现位置 | 状态 |
|---|---|---|
| `src/minicodex/shell.py` | §13(完整 189 行) | ✅ |
| `src/minicodex/tools.py` | §13.1(改动部分),完整版在交付代码里 | ✅ |
| `src/minicodex/__main__.py` | §13.1(一行改动:`DEFAULT_TOOLS`→`default_tools()`) | ✅ |
| `tests/fixtures/stubborn.py` | §5.3(完整) | ✅ |
| `tests/test_shell.py` | §14.2(关键片段),完整版 20 个测试在交付代码里 | ✅ |

---

## §16 codex 是怎么做的

codex 的 shell 执行不是一个函数,是一整个子系统:`codex-rs/execpolicy/`(命令
安全策略)、`codex-rs/core/src/exec.rs`(执行本体)、外加沙箱层
(`codex-rs/linux-sandbox/`、macOS 用 Seatbelt)。这一章只做了其中最基础的
一层——超时、进程组、输出截断、环境隔离——安全审批和沙箱是 Ch05 的内容。

**进程组的用法几乎一致。** `exec.rs` 里子进程同样用 `setsid`/`process_group`
风格的手法启动,杀的时候对整个组下手,理由和这一章一样:`shell=True` 或
等价的 shell 派生场景下,信号必须能传到真正干活的孙子进程。

**输出截断也是双端保留。** codex 的执行结果里有 `total_output_bytes` 和
截断标记,前端展示时同样是"看得到开头,看得到结尾,中间有省略号"——直觉
在两边独立出现,不是巧合,是同一个问题只有一种像样的解法。

**环境变量走的也是白名单而不是继承。** `exec.rs` 里对子进程的环境做了
显式过滤,不是把 `std::env::vars()` 原样传下去。

**持久 shell 会话是一个独立特性**(`unified_exec`),比这一章 `_handle_cd`
的拦截复杂得多——一个真正长期存活的 shell 进程,命令通过 stdin 喂进去、
从 stdout 里用哨兵标记切割出每次调用的结果。这一章刻意没有做到那一步,
原因写在 §10.2 里:那是完全不同量级的机制,提前做只会带来这一章不需要的
复杂度。

---

## §17 回头看：这一章撞到了什么

| 编号 | 故障 | 怎么暴露的 | 挡住它的东西 |
|---|---|---|---|
| F02-01 | 命令一直不返回,Agent 跟着挂死 | 🔵 长跑(手动等到超时) | `asyncio.wait_for` + 超时预算 |
| F02-02 | 100MB 输出撑爆内存 | 🟢 主动边界测试(`yes \| head -c`) | 增量读取 + 硬上限,不用 `communicate()` |
| F02-03 | 只保留结尾,丢掉真正的错误原因 | 🟡 静默(程序正确运行,产出误导模型的内容) | 头+尾双端截断 |
| F02-04 | 交互式命令等 stdin 永远等不到 | 🔵 长跑 | `stdin=DEVNULL` |
| F02-05 | `cd`/`export` 在下一次调用时消失 | 🟡 静默(每次都"成功",效果却没发生) | 拦截 `cd`,自己维护 `cwd` |
| F02-06 | `&` 立刻返回,真正的进程完全失控 | 🟢 主动边界测试 | 明确拒绝,而不是假装支持 |
| F02-07 | 非 UTF-8 字节让整个读取崩溃 | 🔴 崩溃(`UnicodeDecodeError`) | `errors="replace"` |
| F02-08 | 超时后进程仍在系统里跑 | 🟢 主动边界测试(事后 `ps aux`) | 进程组 + `os.killpg` |
| F02-09 | 子进程能看见宿主的全部环境变量,包括密钥 | 🟣 review(Ch01 加了 key 之后回头看) | 环境变量白名单 |
| F02-10 | Windows 上 `start_new_session`/`killpg` 不存在 | ⚪ 无法在本沙箱实测,靠文档 | 平台分支(未实现,见 §18) |
| F02-11 | 截断切在多字节字符中间 | 🟢 主动边界测试 | 按字符(`str`)截断,不按字节 |
| F02-12 | 命令失败但输出为空,模型看不出发生了什么 | 🟡 静默 | 显式附加退出码 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 review · ⚫ 用户报告 ·
⚪ lint/类型

**这一章 🟡 静默类占了三分之一**,比第 0、1 两章的比例都高——原因是操作系统
层面的"部分正确"特别多:超时逻辑跑对了一半(只对会输出的命令生效)、`cd`
"成功"了却什么都没变、命令"失败"了却什么都不说。这类故障的共同点是**程序
从不主动报错**,唯一的发现手段是拿着具体的输入去撞,然后亲眼检查结果。

**另外三分之一是主动边界测试(🟢)**,而不是等它自己出问题——100MB 输出、
非法字节、`&`、多字节截断,没有一个是"跑着跑着突然发现的",全部是"想到
这里可能有问题,专门写一个命令去试"。这是这一章和前两章最大的方法论差异:
前两章的 bug 大多是**用真实场景撞出来的**(narrate-then-call、供应商分片),
这一章的 bug 大多是**明知道操作系统有这些边界、主动去踩**的。两种发现方式
都需要,踩不到的边界不会自己报告自己。

---

## §18 尚未做的事，如实列出

- **Windows**：`start_new_session`、`os.killpg` 都是 POSIX 概念,Windows
  上需要 `CREATE_NEW_PROCESS_GROUP` + `subprocess.CTRL_BREAK_EVENT` 这套
  完全不同的机制。这个沙箱是纯 Linux,没有 Windows 环境可以实测,这一章
  选择明确不支持 Windows,而不是写一段没验证过的兼容代码——**没测过的
  分支,和没写的分支一样不可信**,写一段自己都没跑过的 Windows 代码,
  只会让读者误以为它能用。
- **真正的持久 shell 会话**：`_handle_cd` 只覆盖了 `cd` 作为独立命令的
  情况,`cd foo && pytest` 里的 `cd` 依然不会持久化。
- **后台任务**：明确拒绝,不支持。
- **命令安全审批**：现在任何命令都会被执行,包括 `rm -rf /`。这是 Ch05
  的内容——沙箱和审批需要专门的一章,这一章的重点是"进程管理正确",不是
  "进程管理安全"。

---

## 如果你只记住三件事

1. **`shell=True` 意味着你手里的 PID 不是真正干活的那个。** 超时、kill、
   资源清理,只要不对着整个进程组下手,都只是杀死了中间人。
2. **完全不产出内容的命令,是超时逻辑最容易漏掉的情况。** "读到一行才检查
   时钟"这种写法,对着安静的命令直接失效——这类命令(等待、轮询、睡眠)
   偏偏是最需要超时保护的。
3. **测试运行的环境本身可能会掩盖 bug。** pytest 的 stdin、CI 的资源限制、
   沙箱的网络策略,都可能让一个真实场景下必现的问题在测试里消失不见。
   集成测试证明"能跑通",不代表证明"原因是对的"——为此专门断言调用参数
   的 mock 测试,是这一章从真实踩坑里学到的补充手段。

---

## 动手练习

1. 把 `READ_CEILING_CHARS` 改小(比如 100),用一个真实会产出海量文本的命令
   触发它,确认 `give_up == "ceiling"` 分支被正确命中,而不是被 `timeout`
   分支抢先。
2. 给 `ShellSession` 加一个 `history: list[str]` 字段,记录这个会话执行过
   的所有命令(不含输出)。想一想:这个字段该不该被发进 prompt?如果要发,
   放在 `SystemNote` 里还是拼进 `UserMessage`?(提示:回头看 Ch01 的
   `History` 设计,想一想这类"代码知道、模型不知道"的信息属于哪一种消息
   类型。)
3. 故意写一个命令,让它的输出恰好卡在 `HEAD_CHARS` 和 `TAIL_CHARS` 的边界
   上产生一个不完整的多字节字符,验证 `_clip()` 是否真的安全——注意
   `_clip` 操作的是已经 decode 完的 `str`,和 §9.1 里 `read(4096)` 操作
   `bytes` 是两个不同的边界,想清楚为什么这两处的"安全"来自不同的原因。

下一章：Ch03 · 工具描述工程——`run_shell` 能用了,但模型经常传错参数格式。
问题不在代码里,在工具描述怎么写。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 1 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
第 0、1 章的附录（`asyncio.to_thread`、`functools.partial`、`Path` 基础），
这里只讲这一章正文 §15 自述"完整版在交付代码里"的部分。代码摘自
`steps/step02_shell_tool/src/minicodex/`，逐段核对过。

先把范围说死：

1. 本附录只解释第 2 章在 `steps/step02_shell_tool/` 里新增或修改的代码。
   正文 §13 已经给了 `shell.py` 全文（`_clip`/`ShellSession`/`run`/
   `run_shell`），§13.1 给了 `tools.py` 的改动部分——这些**不再重复**。
   这里补 §15 明说"完整版在交付代码里"的 `tools.py`。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。

## S1 · `tools.py` 完整实现

正文 §13.1 只给了改动（`default_tools` 和 `run_shell` 的 partial）。完整
实现：

```python
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


def default_tools() -> dict[str, Any]:
    """Build a fresh tool table with its own `ShellSession`.

    A function instead of a module-level dict: `ShellSession` holds state
    (`cwd`, `env`) that belongs to one conversation, not to the process.  Two
    agents sharing one session would see each other's `cd`.  `functools.partial`
    binds that session into `run_shell` while keeping the signature every tool
    needs -- `Callable[[dict], Awaitable[str]]` -- the same one `read_file`
    already has.
    """
    session = ShellSession()
    return {
        "read_file": read_file,
        "run_shell": functools.partial(_run_shell, session),
    }
```

四个要点：

1. **`_read` 是同步阻塞 IO，`read_file` 是 async 外壳**（第 1 章 F00-09 的
   规则第一次应用）——`await asyncio.to_thread(_read, Path(path))` 把阻塞
   调用丢线程池。docstring 明说："With one tool at a time nobody notices;
   once chapter 8 runs tools concurrently the concurrency quietly turns into
   a queue."
2. **`_read` 自己处理"不存在"和"是目录"两种错误**——第 3 章才把这些搬进
   `paths.resolve()`，这一章还是 `read_file` 内部处理。错误消息直接告诉
   模型"Check the path and try again"（第 3 章的三段式还没出现）。
3. **`default_tools` 是函数不是模块级 dict**——`ShellSession` 持有 cwd/env
   状态，属于一次会话。两个 Agent 共享一个 session 会看到对方的 `cd`。
   `functools.partial(_run_shell, session)` 把 session 焊死，剩下的
   `args` 参数保持 `Callable[[dict], Awaitable[str]]` 签名（和 `read_file`
   一样，Agent 的 `_run_tool` 统一调用）。
4. **`read_file` 没有 partial**——它不需要任何会话状态（第 3 章才给
   `read_file` 绑 `root`）。"只绑真正需要的"在这里第一次出现。

`TOOL_SCHEMAS`（正文 §13.1 给了 run_shell 的片段，这里补 read_file 和完整
结构）：

```python
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
                f"commands are killed after {30} seconds."
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
```

注意两个细节：

- **`f"commands are killed after {30} seconds."` 是硬编码数字**——正文 §15
  文件清点后专门讨论了：第 3 章把它改成 `tool_schemas(timeout)` 参数化
  （"描述里的数字和常量漂移"是第 3 章修的）。这一章还是 `{30}`。
- **`read_file` 的描述只说 "relative to the working directory"**——第 3
  章才改成 "relative to the repository root"（因为 `read_file` 从那时起
  绑 `root`）。

## S2 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| `read_file` 卡住整个循环 | `Path.read_text` 直接调用 | `_read` 同步函数 + `await asyncio.to_thread(_read, path)` |
| 两个 Agent 共享 cwd | `ShellSession` 是模块级 | `default_tools()` 每次新建 session |
| `run_shell` 签名对不上 | 直接传 session 给 handler | `functools.partial(_run_shell, session)` 保持 `Callable[[dict], ...]` |
| 描述里的秒数和常量漂移 | `{30}` 硬编码 | 第 3 章 `tool_schemas(timeout)` 参数化 |
| 读目录报错 | `Path.read_text` 读目录抛异常 | `_read` 先 `is_dir()` 检查 |
