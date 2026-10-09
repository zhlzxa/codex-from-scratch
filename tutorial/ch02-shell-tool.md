# 第 2 章 · 第一个 shell 工具

> **代码**：`steps/step02_shell_tool/`
> **分支**：`feat/shell-tool`
> **产出**：Agent 能跑 `pytest`、`git diff` 这样的命令，而不只是读文件
> **前置**：做完第 1 章。这一章不需要模型、不需要 API key——故障全在操作系统这一层。
> **Windows 读者请注意**：这一章写出的 shell 工具依赖 macOS/Linux 才有的机制，在 Windows 上
> 不能正确运行（§0.3 解释原因和办法）。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

这一章要让程序去"启动别的程序"。先认识几个操作系统层面的词：

- **进程（process）**：一个正在运行的程序。每个进程有一个编号，叫 **PID**。
- **子进程**：一个进程启动的另一个进程。Python 用 `subprocess` 模块（或者 asyncio 里对应的
  函数）启动子进程。子进程再启动的进程，就是**孙子进程**。
- **shell**：就是你平时敲命令的那个程序（Linux/macOS 上通常是 `/bin/sh` 或 `bash`）。
  `shell=True` 的意思是"把这行命令交给 shell 去解释"，这样才能用管道 `|`、`&&`、`cd` 这些
  shell 的语法。代价是：**真正启动的是 shell，你的命令是 shell 的子进程**，也就是你的孙子进程。
- **标准输入/输出/错误（stdin / stdout / stderr）**：每个进程都有三个"通道"。stdin 是它读输入的
  地方（默认是键盘），stdout 是它正常输出的地方，stderr 是它输出错误的地方（默认都是屏幕）。
- **管道（pipe）**：把一个进程的输出接到另一个地方的通道。我们用管道接住子进程的 stdout，
  才能在 Python 里读到它的输出。
- **退出码（exit code）**：第 -1 章说过，0 表示成功，非 0 表示失败。
- **信号（signal）**：操作系统发给进程的通知。`SIGKILL` 是最强硬的一种："立刻结束"，
  进程无法拒绝，也来不及做任何收尾。
- **环境变量**：第 1 章用来放 API key 的那个东西。**子进程默认会继承父进程的全部环境变量。**

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `await asyncio.create_subprocess_shell(命令, ...)` | 以异步的方式启动一个 shell 子进程 |
| `await asyncio.wait_for(某个等待, timeout=秒数)` | 最多等这么久；超时就取消那个等待，抛出 `asyncio.TimeoutError` |
| `bytes` 和 `b"..."` | 字节串：还没有按某种编码"翻译"成文字的原始数据。子进程输出的是字节 |
| `data.decode("utf-8", errors="replace")` | 把字节按 UTF-8 翻译成字符串；遇到无法翻译的字节，换成 `�` 而不是报错 |
| `functools.partial(函数, 参数)` | 造一个新函数：把原函数的前几个参数提前填好 |
| `monkeypatch.setattr(对象, "名字", 新值)` | pytest 提供的工具：测试期间临时替换某个属性，测试结束后自动恢复 |

### 0.3 Windows 读者

这一章靠两样 macOS/Linux 才有的东西来保证"超时的命令一定被杀干净"：**进程组**（§5 讲）和
`os.killpg`。Windows 没有这两样，对应的机制完全不同。在 Windows 上运行这一章的代码，
`os.killpg` 会报 `AttributeError`——而且是在**命令已经执行完之后**才报。

这一章不写 Windows 版本：一段没有测过的兼容代码，和没写一样不可信；而一个"只杀 shell、
不杀孙子进程"的简化版，恰好就是 §5 要消灭的那个 bug。这是 `FAULTS.md` 里的 F02-10，
一个明确记录下来的限制。

**Windows 读者的建议做法：** 从这一章起，在 **WSL**（Windows 自带的 Linux 子系统）里跟做。
安装方法：在 PowerShell（管理员）里运行 `wsl --install`，重启后打开 "Ubuntu"，在里面装 uv、
克隆你的仓库，其余步骤和 Linux 完全一样。

测试里用到进程组和 Linux 命令（`sleep`、`pwd`、`yes`）的那几个，在 Windows 上会被标记为
**跳过**，并写明原因"F02-10"，而不是显示成一堆看不懂的失败。

### 0.4 开分支

```bash
git switch main
git pull
git switch -c feat/shell-tool
```

---

## §1 这一章要做出来的东西

`read_file` 能看代码，但看不出代码对不对。一个真正有用的编码 Agent 得能跑：

```
pytest
git diff
python3 script.py
```

听起来是"在 `tools.py` 里再加一个工具"的事。**实际上这是目前为止最危险的一个工具**：
读文件最多读错东西；跑命令能让整个 Agent 卡死、吃光内存、留下永远杀不掉的进程，
或者悄悄把你的 API key 交给它启动的每一个程序。

---

## §2 定需求，猜故障

| 目标里的词 | 追问 | 需要做的事 |
|---|---|---|
| 跑一条命令 | 怎么跑？ | 启动一个 shell 子进程 |
| 把结果给模型 | 给什么？ | 输出（stdout 和 stderr 合在一起），失败时说明原因 |
| 作为工具 | 模型怎么调用？ | 一个 `run_shell` 工具和它的 schema |

这一次，"会坏在哪"比前两章更好猜：命令是模型写的，它可能写出任何东西；而操作系统有很多
广为人知的边界。动工前能想到的：

| 编号 | 我担心的事 | 怎么判断它成立 |
|---|---|---|
| F02-01 | 命令一直不返回，Agent 跟着卡死 | 跑 `sleep 30`，看要等多久 |
| F02-04 | 命令在等键盘输入（比如只敲了 `python3`），永远等不到 | 跑 `python3`，看会不会卡住 |
| F02-02 | 命令输出巨大，把内存撑爆 | 跑一个输出 100MB 的命令，量内存 |
| F02-07 | 输出里有不是 UTF-8 的字节，解码时崩溃 | 输出一串非法字节 |
| F02-09 | 第 1 章放进环境变量的 API key，被模型跑的命令看到 | 设一个假密钥，跑 `env` |
| F02-05 | `cd` 到别的目录，下一次调用时又回到原处 | 先 `cd`，再 `pwd` |
| F02-06 | 命令末尾加了 `&` 放到后台，没人管它 | 跑一个后台命令，事后看进程还在不在 |
| F02-12 | 命令失败了但什么都没输出，模型不知道发生了什么 | 跑 `exit 1` |
| F02-10 | Windows 上的行为不同 | 已知限制，§0.3 |

和前两章一样，**第一版先不处理这些**，写一个最朴素的版本，再逐条去撞。区别在于这一次
撞的不是某家服务，而是操作系统本身。

---

## §3 最朴素的版本

新建 `src/minicodex/shell.py`，先写一个最简单的：

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

> - `subprocess.run(...)` 启动子进程，**等它结束**，把结果交回来。
> - `capture_output=True`：把 stdout 和 stderr 接住，而不是打印到屏幕上。
> - `text=True`：把输出从字节翻译成字符串。

跑一下（下面的 `>>>` 是在 Python 交互环境里试）：

```
>>> asyncio.run(run_shell({"command": "echo hi"}))
'hi\n'
```

能动。下面开始按 §2 的表去撞。

---

## §4 F02-01 和 F02-04：卡住

先试 F02-04 那种情况——模型写了 `python3`，而不是 `python3 -c "..."`：

```python
>>> asyncio.run(run_shell({"command": "python3"}))
# ... 一直不返回
```

等了八秒，手动按 Ctrl-C 杀掉。**F02-04 成立，F02-01 也一样**：模型的流式回复有网络超时兜底，
但 `subprocess.run` 默认没有任何超时，命令不结束，它就一直等。

### 4.1 加超时，并且不给它键盘

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

> - `timeout=`：超过这么多秒还没结束，就抛出 `subprocess.TimeoutExpired`。
> - `stdin=subprocess.DEVNULL`：子进程默认继承的是**父进程的 stdin**——在终端里那是键盘，
>   永远不会说"输入结束了"。换成 `DEVNULL`（一个永远是空的输入），它一读就读到"结束"，
>   `python3` 交互环境会立刻退出。

```
>>> asyncio.run(run_shell({"command": "python3"}))
''
```

0.01 秒就返回了。再试一个真正会一直跑的命令（为了方便测试，把超时临时改成 1 秒）：

```
>>> asyncio.run(run_shell({"command": "sleep 30"}))
"Error: command timed out after 1s: 'sleep 30'"
```

一秒就返回了。看起来可以结束了。

---

## §5 意外：超时是假的，进程没死

上面只看了返回值，没看操作系统。补一句检查——`ps aux` 列出系统里所有的进程：

```
$ ps aux | grep "sleep 30"
admin    8  0.0  0.0   6192  2152 ?  S  11:14  0:00 sleep 30
```

**`sleep 30` 还活着。** 超时确实触发了，Agent 也拿到了一条错误信息，但**真正的子进程从来没被
杀掉**，它会一直跑到自然结束。如果模型写的是 `sleep 3600`，这个进程会在系统里再活一个小时；
Agent 调用 `run_shell` 一百次，系统里就可能堆着几十个没人管的命令。

这一条（F02-08）§2 没有猜到。

### 5.1 为什么 `timeout=` 杀不掉它

`subprocess.run("sleep 30", shell=True, ...)` 实际启动的不是 `sleep`，是 `/bin/sh -c "sleep 30"`。
`sleep` 是 `/bin/sh` 启动的**孙子进程**。超时后，Python 杀的是它手里的那个进程——也就是
`/bin/sh`。`/bin/sh` 死了，`sleep` 成了没人管的"孤儿"，被系统收养，继续跑。

这不是碰巧，而是 `shell=True` 的必然结果：只要命令里有管道、分号、`&&`，真正干活的进程就
不会是 Python 直接拿着的那一个。

### 5.2 修复：进程组

macOS/Linux 有一个"进程组"的机制，正好解决这个问题：把一个进程和它将来启动的所有子孙进程
放进同一个组，发信号时可以对整个组下手。

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

> - `subprocess.Popen(...)` 启动子进程，但**不等它结束**，立刻返回一个代表它的对象。
> - `stderr=subprocess.STDOUT`：把 stderr 也接到 stdout 的管道里，两者合在一起读。
> - `start_new_session=True`：shell 和它所有的子孙进程共享一个新的进程组，组号正好等于 `proc.pid`。
> - `proc.communicate(timeout=...)`：等它结束并读出全部输出，超时就抛异常。
> - `os.killpg(组号, signal.SIGKILL)`：对整个组发 `SIGKILL`，不管中间隔了几层，全部收到。
> - `proc.wait()`：等进程真正结束，操作系统才会回收它。

```
>>> asyncio.run(run_shell({"command": "sleep 30"}))  # DEFAULT_TIMEOUT = 1
"Error: command timed out after 1s: 'sleep 30'"
$ ps aux | grep "sleep 30"
$                                    ← 空的
```

### 5.3 一个更顽固的测试

普通的 `sleep` 收到 `SIGKILL` 会立刻死。再写一个"不肯配合"的进程，验证这套机制对它也管用。
新建 `tests/fixtures/stubborn.py`：

```python
"""A process that refuses to die quietly: it catches BrokenPipeError (what
a closed read end raises) and keeps writing instead of exiting.  Used to
test that run_shell's timeout path reaches even a child that ignores the
ordinary "the pipe closed" signal and requires SIGKILL."""

import sys
import time

while True:
    try:
        sys.stdout.write("x" * 1000 + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        time.sleep(0.01)
        continue
```

> 读取的一方不再读了，管道关闭，写入的一方就会收到 `BrokenPipeError`——正常的程序会就此退出。
> 这个程序故意接住这个异常，继续写。

```
>>> asyncio.run(run_shell({"command": "python3 tests/fixtures/stubborn.py"}))  # timeout=1
# 1.4 秒后返回一条超时错误
$ ps aux | grep stubborn
$                                    ← 依然是空的
```

`SIGKILL` 没有"好好退出"这回事，进程无法拒绝。**F02-01 和 F02-08 一起修好了。**

---

## §6 F02-02：100MB 输出

下一个：命令正常结束，但输出巨大——一个大项目上的 `pytest -v`，或者模型不小心 `cat` 了一个
巨大的文件。用 `yes | head -c 100000000` 造 100MB 输出，量一下 Agent 进程的内存：

```python
>>> import resource
>>> before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
>>> out = asyncio.run(run_shell({"command": "yes | head -c 100000000"}))  # 100MB
>>> after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
>>> (after - before) / 1024  # MB
286.9
```

> `resource.getrusage(...).ru_maxrss` 是这个进程到目前为止用过的最大内存（Linux 上单位是 KB）。
> `resource` 模块只在 macOS/Linux 上有。

**Agent 进程的内存涨了 287MB**，只为处理一次工具调用。`communicate()` 会把子进程的全部输出
攒进内存，再一次性交出来。**F02-02 成立。**

### 6.1 边读边判断，超过上限就放弃

要避免这笔开销，就不能等命令跑完再处理——得一边读一边数，一旦超过上限就不读了，并杀掉进程。
这就不能再用一次性返回结果的 `communicate()`，得自己一点一点读。第一版用的是最直接的写法：

```python
for line in proc.stdout:
    ...
    if total > MAX_OUTPUT_CHARS * 50:
        break
```

内存确实降下来了（35MB 而不是 287MB）。但这个写法还藏着一个问题，§8 会撞上。

---

## §7 意外：只留结尾，丢的恰好是最有用的

限制了总量，还有一个问题：模型能看到的输出也得有上限（太长会塞满它的上下文），截断时留哪一部分？
直觉是"只留最后 N 个字符"，反正越后面越新。拿一段真实形状的 pytest 输出试试：

```
FAILED tests/test_foo.py::test_one - AssertionError: expected 1, got 2
========================================
tests/test_bar.py::test_0 PASSED
tests/test_bar.py::test_1 PASSED
... (还有 198 条 PASSED)
========================================
1 failed, 200 passed in 3.21s
```

只留最后 20 行：

```
>>> "FAILED" in tail_only
False
>>> "AssertionError" in tail_only
False
```

**失败的原因整个消失了。** 模型看到的是一堆 PASSED 加一行"1 failed"——它知道有一个测试失败，
但不知道是哪一个，更不知道为什么。程序完全正确地运行、完全正确地截断，产出的东西却对模型
毫无用处。这是本章第一个 🟡 静默故障（F02-03），§2 没有想到。

### 7.1 头 + 尾

```python
if len(text) <= MAX_OUTPUT_CHARS:
    return text
omitted = len(text) - HEAD_CHARS - TAIL_CHARS
return text[:HEAD_CHARS] + f"\n... ({omitted} characters omitted) ...\n" + text[-TAIL_CHARS:]
```

同一段输出：

```
>>> "FAILED" in head_and_tail
True
>>> "AssertionError" in head_and_tail
True
>>> "1 failed, 200 passed" in head_and_tail
True
```

三样都在。pytest 把失败原因放在前面、总结放在最后，中间才是大段可以丢的 PASSED。
这不是巧合，大多数命令行工具都是这个习惯：**开头说错了什么，中间是过程，结尾是结论。**
只留一端，必然丢掉另一端。

还有一个细节（F02-11）：截断时会不会把一个多字节字符（比如一个汉字，UTF-8 里占三个字节）
切成两半？这里切的是**已经解码好的 `str`**，Python 的字符串按字符计数，不是按字节，
所以不会。§9 会看到，在**字节**上切，才需要当心。

---

## §8 拼起来，然后被 ruff 拦住

到这里已经处理了超时、进程组、大输出、头尾截断。整理一下：

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

> `time.monotonic()` 返回一个只会往前走的时钟读数，专门用来算"过了多久"。

跑 `ruff check`：

```
ASYNC220 Async functions should not create subprocesses with blocking methods
   --> src/minicodex/shell.py:XX
    |
    |         proc = subprocess.Popen(
    |                ^^^^^^^^^^^^^^^^
```

**ruff 是对的。** `run_shell` 是 `async def`，而 `subprocess.Popen` 和 `for line in proc.stdout`
都是普通的、会卡住的调用。第 0 章 `read_file` 用 `asyncio.to_thread` 包住 `Path.read_text()`，
是同一个道理：在 `async def` 里卡住，卡住的是整个事件循环。

### 8.1 换成 asyncio 自己的子进程

asyncio 有自己的子进程函数 `asyncio.create_subprocess_shell`。换过去之后，读取也跟着换：
`proc.stdout.read(...)` 是可以 `await` 的，外面套一层 `asyncio.wait_for()` 就有了超时：

```python
proc = await asyncio.create_subprocess_shell(
    command,
    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    stdin=asyncio.subprocess.DEVNULL, start_new_session=True,
)
...
try:
    chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
except asyncio.TimeoutError:
    give_up = "timeout"
```

### 8.2 这时才发现：之前的超时，对"安静的命令"完全无效

换写法的时候回头看了一眼，发现 §8 那个 `for line in proc.stdout` 版本一直带着一个没暴露的 bug。
用一个**什么都不输出**的命令单独测一次：

```
>>> DEFAULT_TIMEOUT = 1
>>> start = time.time()
>>> asyncio.run(run_shell({"command": "sleep 30"}))
>>> time.time() - start
30.002
```

**超时形同虚设，整整等了 30 秒。** `for line in proc.stdout` 会一直卡在"等下一行"上，
循环体里检查时钟的那句代码，只有在**读到一行之后**才会执行。`sleep 30` 什么都不输出，
检查时钟的那句一次都没机会执行。

它没在 §5 被发现，是因为 §5.3 的顽固进程一直在输出，每输出一行就检查一次时钟。**只有完全
安静的命令才会踩中**——而 `sleep`、等待、轮询这类安静的命令，恰恰是最需要超时保护的。

换成 `asyncio.wait_for(...)` 之后，这个问题自动消失了：`wait_for` 能取消一个正在等待、而且
永远等不到东西的 `await`。**被 lint 逼着换成正确的异步写法，结果反而更简单**，还顺手修掉了
一个 bug。

---

## §9 换到 asyncio 之后，又撞了三个坑

新写法更干净，但 asyncio 的子进程有自己的脾气。

### 9.1 按行读有一个隐藏的长度上限

第一次换写法时用的是 `readline()`（一次读一行）。`asyncio` 的 `readline()` 在找到换行符之前
会一直攒着，内部有个默认 64KB 的上限。跑一个一整行、没有换行、超过 64KB 的输出：

```python
script = "print('你好世界' * 10000, end='')"  # 40,000 个字符不换行，UTF-8 编码后 120,000 字节
```

```
asyncio.exceptions.LimitOverrunError: Separator is not found, and chunk
exceed the limit
```

**`readline()` 崩了。** 这种输出并不罕见——有的工具会把一整个 JSON 打印成一行，或者用
`print(..., end="")` 拼进度条。

修法是不再按行读，改成每次读固定的字节数：

```python
chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
```

`read(4096)` 不管有没有换行，有多少读多少（最多 4096 字节），没有上限问题。代价是每一片是
任意切开的**字节**，可能把一个汉字的三个字节切到两片里。但这没关系：所有片先用 `b"".join(...)`
拼起来，**拼完之后才解码一次**，切口在哪里无关紧要。

### 9.2 偶尔冒出来的 "Event loop is closed"

修完之后，连着跑三次同一个测试脚本：

```
$ python3 verify.py; python3 verify.py; python3 verify.py
...
Exception ignored in: <function BaseSubprocessTransport.__del__ at 0x...>
Traceback (most recent call last):
  ...
RuntimeError: Event loop is closed
```

三次里出现一次。**不是每次都发生**——这类时有时无的问题最容易被忽略。

原因是 `asyncio` 的子进程对象没有公开的"关闭"方法，它内部持有的连接对象要等 Python 的
垃圾回收来清理。如果垃圾回收恰好发生在 `asyncio.run()` 结束、事件循环已经关闭之后，
清理动作就无处可去，于是打印出这一段吓人的报错。

修法是不等垃圾回收，在确定进程已经结束后，自己关掉它：

```python
finally:
    proc._transport.close()  # type: ignore[attr-defined]
```

`_transport` 是一个私有属性（以下划线开头，本来不该从外面碰）。asyncio 确实没有提供公开的
关闭方法，这是能找到的最可靠的做法，不是什么最佳实践。`# type: ignore[...]` 告诉类型检查器
"我知道这不是公开属性"。连跑五次，再也没出现。

### 9.3 在 Python 3.11 上，超出上限时会永远卡住

这一条最初写这一章时完全没碰到，因为当时用的是 Python 3.10。换到 3.11 以后才出现：
输出超过上限、放弃读取的那条路径，**会永远卡在最后的 `await proc.wait()` 上**——而那个进程
已经被 `SIGKILL` 杀掉了。

原因藏在 asyncio 的实现里：`proc.wait()` 并不是"进程结束就返回"，而是"进程结束**并且**每个管道
都读到了末尾"才返回。放弃读取的时候，管道里还剩着没读的数据，永远到不了末尾，于是 `wait()`
永远不返回。

对同一个命令（输出 200 万字符，上限 100 万）在不同版本上各试 12 次：

| Python | 卡住的次数 |
|---|---|
| 3.10 | 0 / 12 |
| 3.11 | **12 / 12** |
| 3.12 | 4 / 12 |
| 3.13 | 4 / 12 |

修法是：杀掉进程之后，**先把那个不再读的管道关掉**，再等：

```python
                pipe = proc._transport.get_pipe_transport(1)  # type: ignore[attr-defined]
                if pipe is not None:
                    pipe.close()
```

> `get_pipe_transport(1)` 取出 1 号管道，也就是 stdout 那一个（0 是 stdin，1 是 stdout，2 是 stderr）。

修完之后，每个版本都是 0 / 12，`wait()` 立刻返回。也试过另一个办法——给 `wait()` 本身再加
一个超时——被放弃了：它仍然有 3 到 12 次"卡住"，只是把卡住的时间限制在了超时之内，
而且每次都白白等满超时。**关掉管道修的是原因，给 `wait()` 加超时只是在限制损失。**

> 这一条还牵出一个测试上的教训，§14 会讲：一个**可能会卡住**的调用，检查时间的断言写在它**后面**
> 是没用的——卡住了，断言根本没机会执行。

### 9.4 接进工具表，第一次提交

到这里，`shell.py` 的核心——超时、杀进程组、读取上限、头尾截断、解码、退出码——已经稳定了。
它现在是一个 `ShellSession` 类（§10 会讲为什么它需要是一个类，而不只是一个函数），
外面包一个工具函数 `run_shell(session, args)`。§13 会给出 `shell.py` 的完整代码。

`ShellSession` 带着状态，不能像 `read_file` 那样放在模块级的字典里给所有对话共用。
`tools.py` 改成用一个函数每次造一份新的工具表。这是 `tools.py` 的全部内容：

```python
"""The tools the agent may call, and the schema the model is shown."""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any

from minicodex.shell import ShellSession
from minicodex.shell import run_shell as _run_shell


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

> - **`default_tools()`**：每调用一次，就新建一个 `ShellSession`，造一份新的工具表。docstring 说明了
>   原因：两个 Agent 共用一个会话，会看到对方的 `cd`。
> - **`functools.partial(_run_shell, session)`**：`run_shell` 需要两个参数（会话和 `args`），而 `Agent`
>   调用工具时只传一个 `args`。`partial` 把 `session` 提前填好，造出一个只要 `args` 的新函数——
>   和 `read_file` 的形状一模一样，`agent.py` 一行都不用改。
> - `from minicodex.shell import run_shell as _run_shell`：`as` 给导入的名字换个叫法，
>   避免和本模块里别的名字混淆。
> - **`TOOL_SCHEMAS` 里多了 `run_shell`**：描述里写明了会话内 `cd` 会保留、不支持 `&`、
>   超过 30 秒会被杀掉。注意那个 `{30}` 是写死的数字——它和 `DEFAULT_TIMEOUT` 有可能对不上，
>   下一章会修。

`__main__.py` 里两处改动：import 那行把 `DEFAULT_TOOLS` 换成 `default_tools`，

```python
from minicodex.tools import TOOL_SCHEMAS, default_tools
```

`_ask` 里建 `Agent` 时调用它：

```python
    agent = Agent(llm, default_tools(), recorder=recorder)
```

```bash
git add .
git commit
```

```
feat: add a run_shell tool that cannot hang, flood or orphan

Commands run in their own process group with stdin at EOF, output is read
incrementally with a ceiling and a timeout, and on either limit the whole
group is killed -- killing the shell alone left `sleep 30` running.
Output keeps head and tail: a tail-only clip dropped the pytest failure
at the top.

On 3.11+ the ceiling path hung forever in proc.wait() until the unread
stdout pipe was closed first: 12/12 hangs on 3.11, 4/12 on 3.12 and 3.13,
0/12 after the fix.
```

---

## §10 F02-05：`cd` 不会留下来

模型经常想先 `cd` 到某个子目录，再跑测试：

```python
>>> await run_shell({"command": "cd /tmp"})
>>> await run_shell({"command": "pwd"})
'/tmp/step02_build\n'          # 不是 /tmp
```

**`cd` 没有生效。** 每次调用都是一个全新的 shell 子进程，`cd /tmp` 只改变了那个子进程自己的
工作目录，它一退出，改动就没了。**F02-05 成立。**

### 10.1 为什么传 `cwd=` 参数解决不了

第一反应是：记住一个目录，每次启动子进程时通过 `cwd=` 参数告诉它"从这里开始"：

```python
class ShellSession:
    def __init__(self):
        self.cwd = os.getcwd()
    async def run(self, command):
        proc = ...(cwd=self.cwd, ...)
```

```
>>> session.run("cd /tmp")     # 子 shell 内部 cd 了，但这个信息传不回 Python
>>> session.run("pwd")
'/original/directory\n'        # 还是没变
```

`cwd=` 只能决定"这次从哪里开始"，决定不了"`cd` 之后 Python 怎么知道结果"——`cd` 是 shell
自己内部的命令，它改的是 shell 进程自己的状态，这个改动从来不会传回给 Python。

### 10.2 把 `cd` 拦下来，自己处理

真正的解法是：**不把 `cd` 交给 shell**，在 Python 里认出这条命令，自己算出目标目录，更新 `self.cwd`：

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

> - 返回 `None` 表示"这不是 cd 命令，照常交给 shell"；返回字符串表示"已经处理了，这就是结果"。
> - 只写 `cd` 不带目录，和在终端里一样，回到 HOME 目录。
> - `os.path.expanduser` 把 `~` 换成 HOME 目录；`os.path.join(self.cwd, target)` 处理相对路径
>   （如果 `target` 本身是绝对路径，`join` 会直接用它）；`os.path.normpath` 把 `a/b/../c` 整理成 `a/c`。
> - 目录不存在就报错，而且**不移动**。

```
>>> await session.run("cd /tmp")
''
>>> await session.run("pwd")
'/tmp\n'
>>> await session.run("cd /nonexistent_xyz")
'Error: cd: no such directory: /nonexistent_xyz'
>>> await session.run("pwd")
'/tmp\n'                       # 失败的 cd 不会移动
```

**这个方案有明确的边界**：它只处理 `cd` 单独作为一条命令的情况，不处理 `cd foo && pytest`
这种组合——组合里的 `cd` 依然只在那一次的子 shell 里生效。真正的"持久 shell"需要一个一直活着的
shell 进程，把命令写进它的 stdin，再从 stdout 里切出每一次的结果——那是完全不同量级的东西，
这一章不做，而且把这个边界写进了代码的 docstring 里。

```bash
git add .
git commit -m "fix: keep the working directory between run_shell calls"
```

---

## §11 F02-06：`&` 立刻返回，但进程还在跑

模型有时会想把一个长任务放到后台：

```
>>> start = time.time()
>>> await session.run("nohup sleep 30 > /dev/null 2>&1 &")
>>> time.time() - start
0.011
$ ps aux | grep "sleep 30"
admin  9  0.0  0.0  6192  2152 ?  S  11:18  0:00 sleep 30
```

**shell 立刻返回了，但 `sleep 30` 真的在跑，而且完全不归 `run_shell` 管**——它的输出不在我们读的
管道里，它的退出码不是 `proc.returncode`，超时和 `killpg` 那套逻辑也碰不到它：那套逻辑只在
"命令还没结束"时生效，而 `&` 出去的命令根本不会让 shell 等它。**F02-06 成立。**

### 11.1 被 `&` 出去的进程，真的完全失控了吗？

值得先弄清楚，而不是想当然：既然已经用了 `start_new_session=True`，`&` 出去的进程还在不在
这个进程组里？测出来的结果是：**在**。`&` 只是让 shell 不等它，没有让它离开进程组。
也就是说，只要**记得去杀**，`os.killpg(proc.pid, ...)` 仍然能杀掉它。

**但真正的问题是我们不知道要去杀**：`run_shell` 每次调用完就返回了，没有任何地方记录
"这次在后台留了一个进程，以后要回来处理"。

### 11.2 决定：明确拒绝，而不是假装支持

支持后台任务需要一整套新东西：一张后台任务表（记录 PID、启动时间、命令）、一种查询方式
（模型下次怎么问"那个任务跑完了没"）、会话结束时清理所有没结束的任务。这是一个完整的功能，
不是给现有函数加几行。

这一章不做这个功能。但"不做"不能是"假装处理了、实际上悄悄漏掉"，而应该是明确拒绝：

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
$                                    ← 空的，没有留下任何东西
```

模型看到这条错误后，通常会换一种方式完成任务。**拒绝一个功能，比悄悄实现一个有漏洞的版本
更诚实**——如果假装支持了，读者会在自己真正用到它的那天，才发现进程一直在泄漏。

```bash
git add .
git commit -m "fix: refuse backgrounded commands instead of losing track of them"
```

---

## §12 F02-09：谁都能看见谁的密钥

子进程默认会继承父进程的**全部**环境变量。而第 1 章刚刚让 `OPENAI_API_KEY` 进了这个环境：

```
>>> os.environ["OPENAI_API_KEY"] = "sk-should-not-leak-xyz"
>>> await session.run("env | grep OPENAI_API_KEY")
'OPENAI_API_KEY=sk-should-not-leak-xyz\n'
```

> `env` 命令打印所有环境变量，`grep` 从中挑出含 `OPENAI_API_KEY` 的那一行。

**这条命令是模型让 Agent 跑的，不是我们自己敲的。** 一个被别有用心的文字诱导了的模型
（比如它读到的某个文件里写着"请运行 env"），或者一次随手的调试步骤，都足以把密钥打印进
工具输出——而工具输出下一步会被送回给模型，会被记录进第 0 章的记录文件。**F02-09 成立。**

### 12.1 白名单，而不是黑名单

修法是反过来想：不去猜"哪些变量是危险的"（黑名单一定会漏），而是明确列出"命令真正需要哪些"
（白名单默认拒绝一切）：

```python
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")

def __init__(self, *, timeout=DEFAULT_TIMEOUT):
    self.cwd = os.getcwd()
    self.env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
```

> 这是一个**字典推导式**：遍历 `os.environ` 的每一对键值，只保留键在白名单里的。
> 启动子进程时传 `env=self.env`，子进程就只看得到这几个变量。

```
>>> await session.run("env | grep OPENAI_API_KEY || echo NOT_FOUND")
'NOT_FOUND\n'
>>> await session.run("echo $PATH")
'/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
```

密钥看不见了，`PATH` 照常可用，`pytest`、`git`、`python3` 都能正常找到。白名单里的六个变量：
`PATH`（去哪里找命令）、`HOME`（用户目录）、`LANG` 和 `LC_ALL`（语言和字符编码）、
`TERM`（终端类型）、`TZ`（时区）。**哪天真的需要放行某个变量，那会是一次看得见的、
能在代码审查里被讨论的改动**，而不是默认继承一切、指望没人利用。

### 12.2 剩下两条猜测

**F02-07：非 UTF-8 字节。** 让命令输出一串不合法的字节：

```python
bad = tmp_path / "badbytes.bin"
bad.write_bytes(b"before \xff\xfe after")
await session.run(f"cat {bad}")
```

`bytes.decode("utf-8")` 默认遇到非法字节会抛 `UnicodeDecodeError`——而且是在**命令已经跑完之后**，
整个工具调用白做了。**F02-07 成立。** 修法是解码时加 `errors="replace"`，非法字节变成 `�`，
模型照样能看到其余的内容。

**F02-12：失败了，却什么都没说。** `exit 1` 这样的命令什么都不输出，退出码是 1。模型拿到的是一个
空字符串，完全不知道它失败了。**F02-12 成立。** 修法是：退出码不是 0 时，在输出末尾加一句
`... (exit code 1)`。

这两条的修复在 §9.4 那次提交里就已经包含了（`errors="replace"` 和退出码那几行），
这里只是回来确认它们确实成立、确实修好了。

提交环境变量的修复：

```bash
git add .
git commit -m "fix: give commands an allowlisted environment, not the agent's"
```

---

## §13 完整的 `shell.py`

所有修复加在一起，`src/minicodex/shell.py` 的全部内容：

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

                # ...and then close the pipe we have stopped reading, before
                # waiting.  `proc.wait()` does not resolve when the process
                # dies; it resolves when the process has died AND every pipe
                # has reached EOF.  From CPython's base_subprocess.py:
                #
                #     def _try_finish(self):
                #         if self._returncode is None:
                #             return
                #         if all(p is not None and p.disconnected
                #                for p in self._pipes.values()):
                #             self._call(self._call_connection_lost, None)
                #
                # and `_call_connection_lost` is the only place the futures
                # behind `wait()` are ever resolved.  Giving up on the ceiling
                # leaves unread bytes in the pipe, so stdout never reaches EOF,
                # so `wait()` blocks forever on a process that is already dead.
                #
                # Measured, 12 samples per interpreter, `yes | head -c 2000000`
                # against a 1,000,000-character ceiling:
                #
                #     python 3.10.20   0/12 hang
                #     python 3.11.15  12/12 hang
                #     python 3.12.3    4/12 hang
                #     python 3.13.13   4/12 hang
                #
                # Chapter 2 was first written on 3.10, where this never shows,
                # which is how it went unnoticed.  A read-timeout ceiling on
                # `wait()` was tried and rejected: it still "hung" 3-12 times
                # out of 12, it just capped the damage, and it paid the full
                # timeout every time it fired.  Closing the pipe fixes the
                # cause -- 0/12 on every interpreter, and `wait()` returns in
                # 0.00s.
                pipe = proc._transport.get_pipe_transport(1)  # type: ignore[attr-defined]
                if pipe is not None:
                    pipe.close()
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

从上往下走一遍：

**几个常量。** 默认超时 30 秒；模型最多看到 20,000 个字符（头尾各一半）；读取上限是它的 50 倍，
也就是 100 万个字符——超过就不再读、杀掉进程。`ENV_ALLOWLIST` 是 §12 的白名单，上面的注释写明了
为什么需要它。

**`_clip`**：§7 的头尾截断。docstring 顺带说明了为什么按 `str` 切不会切坏多字节字符（F02-11）。

**`ShellSession.__init__`**：记下当前目录、筛好环境变量、存下超时。超时存成实例属性而不是直接用常量，
是为了让测试可以 `ShellSession(timeout=1)`，不影响别的测试。

**`_handle_cd`**：§10。

**`run`：主体。**

> 1. 先看是不是 `cd`（§10），再看是不是以 `&` 结尾（§11）。两者都会直接返回，不启动子进程。
> 2. `asyncio.create_subprocess_shell(...)` 启动子进程：输出接到管道，stderr 合进 stdout，
>    stdin 是 `DEVNULL`（§4），`start_new_session=True`（§5），工作目录和环境变量来自这个会话。
>    上面的注释说明了为什么不用 `subprocess.Popen`（§8）。
> 3. `assert proc.stdout is not None`：告诉类型检查器"这里一定有 stdout"（因为我们要了管道）。
> 4. 读取循环：每次先算还剩多少时间，没时间了就放弃；否则最多等这么久去读 4096 字节。
>    读到空字节串 `b""` 表示输出结束（EOF）。每读一片就累加长度，超过上限就放弃。
>    注释里记下了 §8.2、§9.1 两次测量。
> 5. 如果放弃了（超时或超上限）：杀掉整个进程组（§5），然后**关掉 stdout 管道**（§9.3），
>    再 `await proc.wait()`。那段长注释把 asyncio 内部的原因和测量数据都记下来了。
> 6. `finally` 里关掉 transport（§9.2）。`try/finally` 保证不管中间出了什么事，这一步都会执行。
> 7. 拼起所有字节，**一次**解码（`errors="replace"`，F02-07），再截断（§7）。
> 8. 最后补一句说明：超时了、超上限了，或者退出码不是 0（F02-12）。模型据此知道发生了什么。

**`run_shell`**：给模型调用的外壳，检查 `command` 参数，然后交给会话。

---

## §14 测试

### 14.1 全部测试

新建 `tests/test_shell.py`，按故障编号分组：

```python
"""What `run_shell` must survive, pinned so it stays survived.

Fault IDs match FAULTS.md.  Every number here was measured directly against
a real subprocess before it was fixed -- see chapter 2 for the traces.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from minicodex.shell import MAX_OUTPUT_CHARS, ShellSession, _clip

STUBBORN = os.path.join(os.path.dirname(__file__), "fixtures", "stubborn.py")

# F02-10.  `run_shell`
# kills process groups with `os.killpg` and `start_new_session`, both of which
# are POSIX-only, and its tests drive a POSIX shell (`sleep`, `cat`, `pwd`,
# `yes`).  On Windows the killpg call raises AttributeError *after the command
# has already run*.
#
# Marked rather than fixed.  A Windows port means job objects instead of
# process groups, which is a real piece of work, not a two-line fallback --
# and a partial fallback (`proc.kill()`) would be worse than the gap, because
# it kills the shell and silently leaves the grandchildren running, which is
# the exact orphan bug F02-08 exists to prevent.
#
# The point of the marker is that the gap now says its own name in the test
# output instead of appearing as seven mysterious red tests.
posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="F02-10: POSIX process groups and POSIX shell builtins; see FAULTS.md",
)


# ---------------------------------------------------------------------------
# F02-01  a command that never returns
# ---------------------------------------------------------------------------


@posix_only
async def test_F02_01_a_hanging_command_is_killed_on_timeout() -> None:
    session = ShellSession(timeout=1)
    start = time.monotonic()
    out = await session.run("sleep 30")
    elapsed = time.monotonic() - start

    assert elapsed < 5, f"took {elapsed}s -- the timeout did not fire"
    assert "timed out after 1" in out


# ---------------------------------------------------------------------------
# F02-02  100MB of output does not blow up memory
# ---------------------------------------------------------------------------


async def _bounded(session: ShellSession, command: str, *, limit: float = 20) -> str:
    """Run a command, but fail rather than hang if it never comes back.

    An assertion on the wall clock placed *after* the call is worth nothing
    against a call that can hang: the assert never runs, and a hanging test
    reports nothing at all until someone gets bored and presses Ctrl-C.  The
    bound has to be on the await itself.  This helper exists because the
    ceiling bug below hid behind exactly that mistake while tests ran on 3.10.
    """
    return await asyncio.wait_for(session.run(command), timeout=limit)


@posix_only
async def test_F02_02_a_firehose_is_capped_not_buffered_whole() -> None:
    session = ShellSession(timeout=10)
    out = await _bounded(session, "yes | head -c 100000000")
    # _clip() guarantees the upper bound regardless of how much the process
    # produced before the read ceiling stopped it.
    assert len(out) <= MAX_OUTPUT_CHARS + 200


@posix_only
async def test_ceiling_does_not_leave_wait_blocked_on_a_dead_process() -> None:
    """Hitting the read ceiling must return, not block forever.

    `proc.wait()` resolves when the process has exited AND every pipe has
    reached EOF.  Giving up on the ceiling leaves unread bytes in stdout, so
    without closing that pipe the wait never resolves -- on a process SIGKILL
    has already killed.

    Measured before the fix, 12 samples per interpreter: 0/12 hang on python
    3.10, 12/12 on 3.11, 4/12 on 3.12 and 3.13.  Chapter 2 was first written
    on 3.10, where it never shows.

    2,000,000 characters rather than the 100,000,000 above: just past the
    1,000,000-character ceiling is the smallest input that reaches the bug,
    and a small one keeps the run fast.
    """
    session = ShellSession(timeout=10)
    out = await _bounded(session, "yes | head -c 2000000")
    assert "killed" in out


# ---------------------------------------------------------------------------
# F02-03  head+tail truncation keeps both ends
# ---------------------------------------------------------------------------


def test_F02_03_clip_keeps_head_and_tail_not_just_tail() -> None:
    body = "FAILED first\n" + ("PASSED\n" * 5000) + "1 failed, 5000 passed"
    clipped = _clip(body)

    assert "FAILED first" in clipped, "the failure reason, at the top, must survive"
    assert "1 failed, 5000 passed" in clipped, "the summary, at the bottom, must survive"
    assert len(clipped) < len(body)


def test_F02_03_short_output_is_not_touched() -> None:
    body = "just a few lines\nnothing to clip\n"
    assert _clip(body) == body


# ---------------------------------------------------------------------------
# F02-04  interactive commands wait on stdin forever
# ---------------------------------------------------------------------------


@posix_only
async def test_F02_04_a_command_reading_stdin_gets_eof_immediately() -> None:
    """`python3` with no `-c` is a REPL that reads stdin.  Without
    `stdin=DEVNULL` this hangs until the timeout; measured directly at the
    full 5-second timeout outside of pytest.  With it, the REPL sees EOF and
    exits at once."""
    session = ShellSession(timeout=5)
    start = time.monotonic()
    await session.run(f"{sys.executable}")
    elapsed = time.monotonic() - start
    assert elapsed < 2, f"took {elapsed}s -- looks like it wasn't given DEVNULL stdin"


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


# ---------------------------------------------------------------------------
# F02-05  cd and export are lost between calls
# ---------------------------------------------------------------------------


@posix_only
async def test_F02_05_cd_persists_to_the_next_call(tmp_path) -> None:
    session = ShellSession()
    await session.run(f"cd {tmp_path}")
    out = await session.run("pwd")
    assert out.strip() == str(tmp_path)


async def test_F02_05_cd_to_a_missing_directory_is_reported_and_does_not_move() -> None:
    session = ShellSession()
    before = session.cwd
    out = await session.run("cd /no/such/directory/at/all")
    assert "no such directory" in out
    assert session.cwd == before


async def test_F02_05_a_second_shell_session_does_not_see_the_first_ones_cd(tmp_path) -> None:
    a = ShellSession()
    b = ShellSession()
    await a.run(f"cd {tmp_path}")
    assert (await b.run("pwd")).strip() != str(tmp_path)


# ---------------------------------------------------------------------------
# F02-06  backgrounded commands are refused, not silently leaked
# ---------------------------------------------------------------------------


async def test_F02_06_a_trailing_ampersand_is_refused() -> None:
    session = ShellSession()
    out = await session.run("sleep 30 &")
    assert "does not support backgrounded" in out


async def test_F02_06_refusing_it_does_not_leave_the_process_running() -> None:
    session = ShellSession()
    await session.run("sleep 5 &")
    await asyncio.sleep(0.5)
    # `pgrep -f` matches against the full command line, which would also
    # match its own invocation if that invocation mentioned the pattern --
    # `[s]leep 5` (bracketing the first letter) is the standard trick to
    # exclude the grep/pgrep process itself from its own match.
    result = await session.run("pgrep -f '[s]leep 5' || echo NONE")
    assert "NONE" in result


# ---------------------------------------------------------------------------
# F02-07  non-UTF-8 bytes must not crash the read
# ---------------------------------------------------------------------------


async def test_F02_07_invalid_utf8_is_replaced_not_raised(tmp_path) -> None:
    bad = tmp_path / "badbytes.bin"
    bad.write_bytes(b"before \xff\xfe after")

    session = ShellSession()
    out = await session.run(f"cat {bad}")  # must not raise UnicodeDecodeError
    assert "before" in out and "after" in out
    assert "�" in out  # the replacement character


# ---------------------------------------------------------------------------
# F02-08  orphaned processes must not outlive the call
# ---------------------------------------------------------------------------


@posix_only
async def test_F02_08_a_timed_out_command_leaves_no_process_behind() -> None:
    """`shell=True` runs the command as a child of `/bin/sh`.  Killing only
    the `Popen`/`Process` object kills the shell, not what it forked --
    measured directly: `sleep 3600` outlived a 1-second timeout and was
    still running afterward.  `os.killpg` on the whole process group is what
    reaches it."""
    session = ShellSession(timeout=1)
    await session.run("sleep 3600")
    await asyncio.sleep(0.5)

    # No live process anywhere on the machine should still be running that
    # command.  The bracket trick keeps this pgrep call from matching its
    # own command line.
    result = await session.run("pgrep -f '[s]leep 3600' || echo NONE")
    assert "NONE" in result


@posix_only
async def test_F02_08_a_stubborn_child_that_ignores_the_pipe_closing_is_still_killed() -> None:
    """A process that catches BrokenPipeError and keeps writing does not
    stop on its own when the read side gives up -- it has to be killed.

    Bounded for the same reason as F02-02: this one reaches the give-up path
    with output still sitting unread in the pipe, so it hangs rather than
    fails if that pipe is not closed before the wait.  The timeout path and
    the ceiling path share the defect and therefore share the bound.
    """
    session = ShellSession(timeout=1)
    out = await _bounded(session, f"{sys.executable} {STUBBORN}")
    assert "killed" in out

    await asyncio.sleep(0.5)
    result = await session.run("pgrep -f '[s]tubborn.py' || echo NONE")
    assert "NONE" in result


# ---------------------------------------------------------------------------
# F02-09  the host's environment (including secrets) is not inherited
# ---------------------------------------------------------------------------


async def test_F02_09_a_variable_outside_the_allowlist_is_not_visible(monkeypatch) -> None:
    monkeypatch.setenv("SOME_SECRET_KEY", "sk-should-not-leak")
    session = ShellSession()
    out = await session.run("echo ${SOME_SECRET_KEY:-NOT_SET}")
    assert "NOT_SET" in out
    assert "sk-should-not-leak" not in out


async def test_F02_09_path_is_still_usable() -> None:
    session = ShellSession()
    out = await session.run("echo $PATH")
    assert out.strip() != ""


# ---------------------------------------------------------------------------
# F02-11  truncation does not split a multi-byte character
# ---------------------------------------------------------------------------


async def test_F02_11_clipping_never_produces_a_decode_error() -> None:
    # More than twice MAX_OUTPUT_CHARS of a 3-byte-per-character string, so
    # the cut point in _clip() is very unlikely to land on a character
    # boundary by luck.
    session = ShellSession(timeout=10)
    script = "print('你好世界' * 10000, end='')"
    out = await session.run(f'{sys.executable} -c "{script}"')

    out.encode("utf-8")  # raises if a surrogate or partial sequence leaked through
    assert len(out) <= MAX_OUTPUT_CHARS + 200


def test_F02_11_clip_slices_by_character_not_by_byte() -> None:
    # _clip operates on `str`, which Python indexes by code point, so a
    # slice boundary can never fall inside a multi-byte character the way a
    # `bytes` slice could.
    text = "你" * (MAX_OUTPUT_CHARS + 100)
    clipped = _clip(text)
    clipped.encode("utf-8")


# ---------------------------------------------------------------------------
# F02-12  a failing command with empty output must say so
# ---------------------------------------------------------------------------


async def test_F02_12_nonzero_exit_with_empty_output_names_the_exit_code() -> None:
    session = ShellSession()
    out = await session.run("exit 1")
    assert "exit code 1" in out


async def test_F02_12_a_successful_silent_command_is_not_confused_with_a_failure() -> None:
    session = ShellSession()
    out = await session.run("true")
    assert "exit code" not in out


# ---------------------------------------------------------------------------
# missing/invalid input
# ---------------------------------------------------------------------------


async def test_run_shell_wrapper_rejects_a_missing_command() -> None:
    from minicodex.shell import run_shell

    session = ShellSession()
    out = await run_shell(session, {})
    assert "needs a" in out
```

逐块说：

**开头的 `posix_only`。** 一个可以复用的"标记"：`pytest.mark.skipif(条件, reason=...)` 在条件成立时
跳过测试。这里的条件是 `sys.platform == "win32"`（在 Windows 上）。上面的注释说明了为什么是"标记"
而不是"修复"（§0.3）。凡是用到进程组或 Linux 命令的测试，都加上了 `@posix_only`。

**F02-01**：`sleep 30` 配 1 秒超时，断言 5 秒内返回，并且输出里说明了超时。

**`_bounded` 和 F02-02。** 这里有一个 §9.3 带来的教训。最初 F02-02 的测试是这么写的：

```python
out = await session.run("yes | head -c 100000000")
assert len(out) <= MAX_OUTPUT_CHARS + 200
```

在 3.11 上，`session.run(...)` 会卡住——而**卡住的测试不会失败，它只是一直不结束**，
直到有人等得不耐烦按 Ctrl-C。后面的断言根本没机会执行。**对一个可能卡住的调用，
时间限制必须加在 `await` 上，而不是写在它后面。** `_bounded` 就是干这个的：用
`asyncio.wait_for` 给整个调用套一个 20 秒的上限，超时就抛异常，测试以"失败"结束，
而不是"永远不结束"。

**`test_ceiling_does_not_leave_wait_blocked_on_a_dead_process`**：专门针对 §9.3。用 200 万字符
（刚过 100 万的上限）——这是能触发那个 bug 的最小输入，小一点跑得快。

**F02-03**：两个不启动子进程的测试，直接测 `_clip`：失败原因和总结都要保留；短输出不动。

**F02-04 的两个测试，以及一个测试环境的"盲区"。** 第一个是集成测试：真的启动 `python3` 交互环境，
断言 2 秒内返回。第二个直接检查传给 `create_subprocess_shell` 的 `stdin` 参数：

> - `monkeypatch.setattr(asyncio, "create_subprocess_shell", spy)`：测试期间，把 asyncio 的这个函数
>   换成 `spy`。`spy` 记下收到的关键字参数（`**kwargs` 收集所有关键字参数成一个字典），
>   再调用真正的函数。测试结束后，pytest 自动换回原来的函数。

**为什么需要第二个？** 这是写这一章时真实撞到的：把 `stdin=DEVNULL` 从代码里删掉，重新跑第一个测试，
**它仍然通过**。原因是在 pytest 里运行时，pytest 进程自己的 stdin 往往已经被重定向了，子进程继承它，
碰巧也会很快返回——不是 bug 被修好了，是测试运行的环境把 bug 藏了起来。第二个测试不依赖运行环境，
直接检查代码有没有传 `DEVNULL`。docstring 里记下了这次测量。

**F02-05**：`cd` 之后 `pwd`；`cd` 到不存在的目录要报错且不移动；两个会话互不影响（这正是
`default_tools()` 每次新建会话的原因）。

**F02-06**：`&` 结尾的命令被拒绝；而且被拒绝之后，系统里不留下任何进程。这里和 F02-08 都要检查"某个进程还在不在"，用的是这样的写法（F02-08 里的一个）：

```python
result = await session.run("pgrep -f '[s]leep 3600' || echo NONE")
```

> `pgrep -f` 按完整的命令行找进程。如果直接写 `pgrep -f 'sleep 3600'`，这条检查命令自己的命令行里就有
> `sleep 3600` 这几个字，会找到它自己。写成 `[s]leep`：作为匹配模式，`[s]` 匹配字母 s，所以能找到
> `sleep 3600`；而检查命令自己的命令行里写的是字面的 `[s]leep`，匹配不上，于是不会找到自己。
> `|| echo NONE`：前一条命令失败（没找到）时才执行后一条。

**F02-07**：§12.2。

**F02-08**：超时后不留进程；§5.3 的顽固进程也能被杀掉（它也用了 `_bounded`，理由见 docstring）。

**F02-09**：白名单外的变量看不到；`PATH` 仍然能用。

**F02-11**：输出一长串汉字，截断后重新编码不会出错；`_clip` 按字符切。

**F02-12**：失败且没输出时，结果里有退出码；成功且没输出时，**不会**被误报成失败。

**最后一个**：`run_shell` 缺少 `command` 参数时，返回一条说明，而不是报错。

### 14.2 把修复改回去

| 改回去 | 结果 |
|---|---|
| `errors="replace"` → `"strict"` | `test_F02_07_invalid_utf8_is_replaced_not_raised` 失败，`UnicodeDecodeError` |
| `os.killpg` → `proc.kill()` | 两个 F02-08 测试失败，进程残留 |
| 去掉 `stdin=DEVNULL` | 集成测试**没有**失败（§14.1 的盲区），检查参数的那个正确地失败了 |
| 去掉 `_handle_cd` | 两个 F02-05 测试失败 |
| 禁用 `_clip` | F02-02、F02-03、F02-11 的测试全部失败 |

**`stdin=DEVNULL` 那一行不是一次失败的验证，而是一次成功的发现**：它证明了 §14.1 说的盲区是真的，
而不是凭空担心。

（这张表来自写作时在 Linux 上的实测。）

### 14.3 全部跑一遍

```
$ uv run pytest
.............................................................sss..s.s... [ 86%]
..ss.......                                                              [100%]
76 passed, 7 skipped in 9.41s
```

（Windows 上实测：7 个用到进程组和 Linux 命令的测试被标记跳过，原因写着 F02-10。在 macOS/Linux 上，
它们会全部运行并通过。）

```bash
git add .
git commit -m "test: pin every shell fault, bounded so a hang fails instead of stalling"
```

---

## §15 回顾：猜对了几条

| 编号 | 问题 | 结果 | 如果不防，它会怎么暴露 | 挡住它的东西 |
|---|---|---|---|---|
| F02-01 | 命令一直不返回，Agent 跟着卡死 | ✅ 撞上（§4） | 🔵 长时间运行才出现 | `asyncio.wait_for` + 超时 |
| F02-04 | 交互式命令等键盘输入，永远等不到 | ✅ 撞上（§4） | 🔵 | `stdin=DEVNULL` |
| F02-02 | 100MB 输出撑爆内存 | ✅ 撞上（§6） | 🟢 主动边界测试 | 边读边数，超上限就停 |
| F02-07 | 非 UTF-8 字节让解码崩溃 | ✅ 撞上（§12.2） | 🔴 | `errors="replace"` |
| F02-09 | 子进程能看见 API key | ✅ 撞上（§12） | 🟣 回头审查时发现 | 环境变量白名单 |
| F02-05 | `cd` 在下一次调用时消失 | ✅ 撞上（§10） | 🟡 每次都"成功"，效果却没发生 | 拦下 `cd`，自己维护目录 |
| F02-06 | `&` 放到后台的进程没人管 | ✅ 撞上（§11） | 🟢 | 明确拒绝 |
| F02-12 | 失败了却什么都没说 | ✅ 撞上（§12.2） | 🟡 | 附上退出码 |
| F02-10 | Windows 上行为不同 | 🛡 已知限制（§0.3） | ⚪ → 🔴 在 Windows 上运行就崩 | 测试标记跳过并写明原因；建议用 WSL |
| F02-08 | *（没猜到）* 超时后进程还在跑 | ⚠️ 意外（§5） | 🟢 事后 `ps aux` 才看到 | 进程组 + `os.killpg` |
| F02-03 | *（没猜到）* 只留结尾，丢了错误原因 | ⚠️ 意外（§7） | 🟡 | 头尾截断 |
| F02-11 | 截断切坏多字节字符 | 🛡 按字符切，不会发生（§7） | 🟢 | 解码后再截断 |
| — | *（没猜到）* 安静的命令让超时失效 | ⚠️ 意外（§8.2） | 🔵 | `asyncio.wait_for` |
| — | *（没猜到）* 按行读有 64KB 上限 | ⚠️ 意外（§9.1） | 🔴 | `read(4096)` |
| — | *（没猜到）* 时有时无的 "Event loop is closed" | ⚠️ 意外（§9.2） | 🟠 看到报错才发现 | 手动关闭 transport |
| — | *（没猜到）* 3.11 上超出上限时永远卡住 | ⚠️ 意外（§9.3） | 🔵 | 先关管道，再等 |
| — | *（没猜到）* 测试环境把 stdin 的 bug 藏起来了 | ⚠️ 意外（§14.1） | 🟡 | 直接检查传入的参数 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 主动边界测试 · 🔵 长时间运行才出现 · 🟠 看日志发现 · 🟣 代码审查 ·
⚫ 用户报告 · ⚪ lint/类型检查

**猜的八条全中了**，因为它们都是操作系统广为人知的边界。**但没猜到的更多**，而且大多藏在"修好了"
之后：超时"修好了"，进程却没死；换了写法"修好了"，又冒出三个新问题。

这一章还有一个前两章没有的特点：**最狡猾的问题出在"测试本身"**。一个是测试运行环境藏起了 stdin
的 bug，一个是"可能卡住的调用"的测试只会卡住、不会失败。**测试也是代码，也会有盲区**——
第 -1 章那条"把修复改回去看测试红不红"，正是为了抓住这种盲区。

---

## §16 交给 GitHub

```bash
git push -u origin feat/shell-tool
```

开 PR、等 CI、自己审查、合并，和前两章一样。

> **CI 在 Linux 上跑**（第 -1 章的配置写的是 `ubuntu-latest`），所以那 7 个在 Windows 上被跳过的测试，
> 在 CI 里会真的运行。这正好补上了 Windows 读者本地看不到的那部分。

这一章的提交：

```
test: pin every shell fault, bounded so a hang fails instead of stalling
fix: give commands an allowlisted environment, not the agent's
fix: refuse backgrounded commands instead of losing track of them
fix: keep the working directory between run_shell calls
feat: add a run_shell tool that cannot hang, flood or orphan
```

> 测试集中在最后一个提交里，是因为这一章的实现改了好几次写法（`subprocess.run` → `Popen` → asyncio），
> 中间版本的测试写了也会被推翻。**更好的做法是每个修复都带着它的测试一起提交**，前两章就是这么做的；
> 这一章如实记录了没这么做的样子。

---

## §17 本章给 CI 加了什么

**什么都没加。** 新测试由现有的"跑测试"那一步自动运行。

---

## §18 三条主线各自留下了什么

### 主线 A · 需求变代码

**写一个最朴素的版本，然后逐条去撞。** 这一章的 `run_shell` 从 5 行长到两百多行，每一行都能对上
一次具体的测量。

**知道边界在哪，并把它写下来。** `cd` 只处理单独的一条命令、不支持后台任务、不支持 Windows——
这三个边界都写进了代码的 docstring 或错误信息里，而不是留给读者自己发现。

| 东西 | 决定 | 理由 |
|---|---|---|
| `ShellSession` 类 | **做** | 有状态（目录、环境）要在多次调用间保留 |
| `default_tools()` 函数 | **做** | 每个对话一个会话，不能共用 |
| 持久 shell 进程 | **不做** | 完全不同量级的机制，这一章的需求用不到 |
| 后台任务 | **不做**，明确拒绝 | 需要一整套新功能，半成品只会泄漏进程 |
| Windows 支持 | **不做**，明确标记 | 没测过的代码不可信；简化版正是要消灭的 bug |

### 主线 B · 工程化交付

| 动作 | 本章的规则 |
|---|---|
| lint 报错 | 不关掉它，而是理解它——ASYNC220 逼出了更简单、更正确的写法 |
| 平台限制 | 测试用 `skipif` 标记并写明故障编号，而不是让它显示成看不懂的失败 |
| 可能卡住的测试 | 时间限制加在 `await` 上 |
| 提交 | 最好每个修复带着自己的测试；做不到时，如实说明 |

### 主线 C · 故障

**第一招：`shell=True` 意味着你手里的 PID 不是真正干活的那个。** 超时、杀进程、清理资源，
不对着整个进程组下手，就只是杀死了中间人。

**第二招：什么都不输出的命令，最容易让超时失效。** "读到一行才检查时间"的写法，对安静的命令完全无效。

**第三招：测试运行的环境，可能把 bug 藏起来。** 集成测试证明"能跑通"，不代表证明"原因是对的"。
必要时，直接检查代码传了什么参数。

**第四招：在不同的环境里跑一遍。** 3.10 上从不出现的卡死，3.11 上次次出现。

---

## 如果你只记住三件事

1. **`shell=True` 意味着你手里的 PID 不是真正干活的那个。** 杀进程要杀整个进程组。
2. **完全不输出内容的命令，是超时逻辑最容易漏掉的情况**——而等待、轮询、睡眠这类命令偏偏最需要超时。
3. **测试也会有盲区。** 运行环境会藏起 bug；可能卡住的调用，测试只会卡住而不会失败。

---

## 动手

```bash
cd steps/step02_shell_tool
uv sync --all-extras
uv run pytest
```

**建议自己做一遍的三件事：**

1. 把 `READ_CEILING_CHARS` 改小（比如 100），用一个会输出大量文字的命令触发它，确认走的是"超上限"那条路，
   而不是"超时"那条。
2. 给 `ShellSession` 加一个 `history: list[str]` 字段，记录执行过的所有命令。想一想：要不要把它告诉模型？
   如果要，应该用第 1 章 `History` 里的哪一种消息？（提示：代码知道、模型不知道的事。）
3. 在 macOS/Linux 上，把 §9.3 的"关管道"那几行注释掉，用 Python 3.11 跑
   `uv run --python 3.11 pytest -k ceiling`，看 `_bounded` 怎么把"卡住"变成"失败"。

---

## 选读 · codex 是怎么做的

> 基于写作时（2026 年）的 codex 仓库，以后可能会变。不读不影响后面的内容。

codex 的命令执行不是一个函数，而是一整个子系统：`codex-rs/execpolicy/`（命令安全策略）、
`codex-rs/core/src/exec.rs`（执行本体），外加沙箱层（Linux 上的 `codex-rs/linux-sandbox/`，
macOS 上用 Seatbelt）。这一章只做了最基础的一层——超时、进程组、输出截断、环境隔离。
安全审批和沙箱是后面专门的一章。

**进程组的用法几乎一样。** `exec.rs` 里子进程同样以新的进程组启动，杀的时候对整个组下手。

**输出截断也是头尾都留。** 执行结果里有输出的总字节数和截断标记，展示时同样是"看得到开头、看得到结尾、
中间省略"。两边独立得出同一个做法，说明这个问题只有一种像样的解法。

**环境变量也是显式过滤的**，不是把全部环境原样传下去。

**持久 shell 会话是一个单独的功能**（`unified_exec`）：一个一直活着的 shell 进程，命令从 stdin 喂进去，
从 stdout 里用特殊标记切出每次的结果。这正是 §10.2 说的"完全不同量级的机制"。

---

**下一章**：[工具描述工程](ch03-tool-descriptions.md)——`run_shell` 能用了，但模型经常传错参数。
问题不在代码里，在工具描述怎么写。

---

# 附录 · 对照检查

## T1 · 每个文件是在哪一节写的

本章结束时，你的项目内容应该和 `steps/step02_shell_tool/` 一致（测试函数的先后顺序可以不同）。

| 文件 | 在哪写的 |
|---|---|
| `src/minicodex/shell.py` | §3 起逐步长成，完整版见 §13 |
| `src/minicodex/tools.py` | §9.4 |
| `src/minicodex/__main__.py` | §9.4（两行） |
| `tests/fixtures/stubborn.py` | §5.3 |
| `tests/test_shell.py` | §14.1 |

其余文件这一章没有改动。

## T2 · 常见报错对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| Windows 上 `AttributeError: module 'os' has no attribute 'killpg'` | 进程组是 macOS/Linux 才有的机制（F02-10） | 在 WSL 里运行（§0.3） |
| 测试显示 `7 skipped` | 在 Windows 上，用到进程组的测试被标记跳过 | 正常现象；CI（Linux）会运行它们 |
| ruff 报 `ASYNC220` | 在 `async def` 里用了 `subprocess.Popen` | 换成 `asyncio.create_subprocess_shell`（§8.1） |
| `LimitOverrunError: Separator is not found` | 用 `readline()` 读了一行超长的输出 | 改用 `read(4096)`（§9.1） |
| 偶尔出现 `RuntimeError: Event loop is closed` | 子进程的 transport 没有手动关闭 | `finally` 里 `proc._transport.close()`（§9.2） |
| 测试一直不结束（3.11 及以上） | 超出上限后，`wait()` 在等一个还有数据没读的管道 | 杀进程后先关 stdout 管道（§9.3） |
| `cd` 之后 `pwd` 还是原来的目录 | 每次调用都是新的子进程 | 用 `ShellSession` 拦下 `cd`（§10） |
| `UnicodeDecodeError` | 输出里有非 UTF-8 字节 | `decode("utf-8", errors="replace")` |
