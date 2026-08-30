# 第 20 章 · 沙箱：把"允许"从一个判断变成一条边界

> **代码**：`steps/step20_sandbox/`
> **分支**：`feat/linux-sandbox`
> **产出**：`src/minicodex/sandbox.py`（bubblewrap 命令行生成）、
> `src/minicodex/tenancy.py`（`Owner`，归属的那条缝）、
> `shell.py` 两行、`policy.py` 一个分支、
> `probe_sandbox.py` 六段、`probe_mutations_ch20.py` 26 条
> **你需要**：本章 48 个测试里 46 个在任何平台离线跑，2 个需要真的 `bwrap`。
> `probe_sandbox.py` **只在 Linux 上有意义**——这本书写在 Windows 上，
> 下面每一个数字都来自 WSL2 Ubuntu，内核 6.6.87.2，bubblewrap 0.9.0。
>
> **阅读顺序在第 15 章之前，编号在其之后。** 理由和 Ch16/Ch17/Ch18/Ch19 一样。

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
$ wsl -d Ubuntu -- bash -lc "cd steps/step20_sandbox && python3 probe_sandbox.py escape"

--- escape --------------------------------------------------------------
F05-04 -- an interpreter writing in read-only mode
  chapter 5's note: 'Tokenising is perfect here and tells you nothing'

  no sandbox        exit=0  file exists: True
  read-only         exit=1  file exists: False
                    OSError: [Errno 30] Read-only file system: '/tmp/tmphv6mkao0/outside.txt
  workspace-write   exit=0  wrote inside: True
  workspace-write   exit=1  wrote outside: False

F05-05 -- network, 'recognised rather than blocked'
  read-only         exit=6  (curl 6 = could not resolve host)
  read-only +net    exit=0  (0 = fetched)
```

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
--- binds ---------------------------------------------------------------
  the smallest bind set that still has a shell in it

  exit=1   --ro-bind <ws> <ws>                                bwrap: execvp /bin/sh: No such fil
  exit=1   --ro-bind <ws> <ws> --ro-bind /bin /bin            bwrap: execvp /bin/sh: No such fil
  exit=0   --ro-bind <ws> <ws> --ro-bind /bin /bin --ro-bind /lib /li ok
  exit=0   --ro-bind / /                                      ok
```

"只绑工作区"不是更严格的沙箱，**是一个坏掉的沙箱**：`/bin/sh` 都不在里面，
什么都跑不起来。加上 `/bin` 还不够，因为 `sh` 要动态链接；要到
`/bin` + `/lib` + `/lib64` 才有一个能用的 shell。

于是形状定下来了：**把 `/` 绑成只读，然后在上面打可写的洞。**

```python
argv += ["--ro-bind", _ROOT, _ROOT]
for path in self.spec.writable():
    argv += ["--bind", str(path), str(path)]
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

```python
# A plain string, and `Path("/")` was tried first.  It is wrong for a reason
# that only shows up off Linux ... The root of a bwrap namespace is a *Linux
# path literal*, not a path on whatever host happens to be constructing the
# command line, and the type has to say so.
_ROOT = "/"
```

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
--- net -----------------------------------------------------------------
  read-only        network=False  blocked (curl 6)
  read-only        network=True   reached
  workspace-write  network=False  blocked (curl 6)
  workspace-write  network=True   reached
```

> **和 codex 的差距**：codex 的网络控制远不止开关。`network-proxy`（15,000 行）
> 是一个 MITM 代理：自签 CA、按域名授权、SOCKS5，还有一个**凭据代理**——
> GitHub 和 OpenAI 的真实密钥由代理注入，**沙箱里的进程从头到尾看不到它们**
> （`network-proxy/src/credential_broker/providers/`）。
> 这里只有 `--unshare-net`。那是一个开关，不是一个策略。

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
--- exits ---------------------------------------------------------------
  every setup failure below exits 1 -- so does a command that returns 1

  exit=1   chdir outside the bind set     detected as: wrapper
  exit=1   bind a source that is absent   detected as: wrapper
  exit=1   interpreter unreachable        detected as: wrapper
  exit=1   an unknown flag                detected as: wrapper
  exit=42  the command exits 42           detected as: command
  exit=0   the command succeeds           detected as: command
```

**bwrap 建不起沙箱是 exit 1，命令跑完了返回 1 也是 exit 1。**
退出码分不开这两件事。

为什么这要紧：模型拿到 "exit code 1" 加一段看起来像命令输出的文字，
会把**容器化失败**读成**自己的命令写错了**，然后重试。这就是 F05-09 的形状
（沙箱拒绝被当成业务错误疯狂重试），从一扇新的门进来。

所以只能靠前缀：

```python
def setup_failure(output: str, returncode: int | None) -> str | None:
    if returncode != 1:
        return None
    first = output.lstrip().split("\n", 1)[0]
    if first.startswith(_BWRAP_ERROR_PREFIX):
        return first[len(_BWRAP_ERROR_PREFIX) :].strip()
    return None
```

这是个启发式，它的边界要说明白：一条命令如果自己的输出以 `bwrap: ` 开头，
就会被误判。更好的做法是 `--info-fd`，用一根独立的管道拿状态——
代价是 `shell.run` 里多一个读取器，为一个实测发生零次的情况。
留作练习，并且**在 README 里写明了这是个已知的洞**，而不是假装它不存在。

`shell.run` 里对应的分支：

```python
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

---

## §6 F20-06：超时必须穿过包装层

第 2 章的 F02-01 给命令加了超时，靠的是 `_kill_group` 杀掉整个进程组——
因为 `shell=True` 意味着命令是 shell 的子进程，只杀 shell 会留下一个没人看管的孙子。

现在中间多了一层 bwrap。杀 bwrap 会带走里面的东西吗？

```
--- kill ----------------------------------------------------------------
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

---

## §7 F20-07：两套机制，一个意图

第 16 章为了让 `read_file` 够得到 `~/.minicodex/memories`，给
`paths.resolve()` 加了 `extra_roots`。那是一个**Python 层**的边界检查。

现在 `run_shell` 有了一个**内核层**的边界。两者互不知道对方存在。

后果很直接：一个路径加进了其中一个、没加进另一个，就会得到一个
**`read_file` 读得到、`cat` 读不到的文件**——或者反过来。这不是 bug，
但极易被误解成 bug，所以代码里写清楚了：

```python
`read_roots` carries chapter 16's extra read-only root. That one is worth
a sentence: `paths.resolve(..., extra_roots=...)` bounds `read_file` in
*Python*, and the sandbox bounds `run_shell` in the *kernel*. They are two
mechanisms enforcing one intention, and neither knows about the other.
```

两个细节是量出来的：

**用 `--ro-bind-try` 不是 `--ro-bind`。** 实测绑一个不存在的源路径，
bwrap 直接拒绝启动：

```
bwrap: Can't find source path /definitely/not/here: No such file or directory
```

一个从没写过记忆的用户，`~/.minicodex/memories` 是不存在的。用 `--ro-bind`
的话，他得到的不是"没有记忆"，是**没有 shell**。

**只读的绑定要放在可写的洞之后。** bwrap 按顺序应用，后面的覆盖前面的。
一个位于工作区内部的只读根，如果先绑，会被随后的可写工作区**重新挂成可写**——
悄悄地把你刚加的那道墙拆了。测试直接盯着顺序：

```python
assert argv.index("--bind") < argv.index("--ro-bind-try")
```

---

## §8 意外的回报：沙箱换来的是**更少的确认框**

这一条我事先没想到，是写到 `policy.py` 才发现的。

看第 5 章那张表：

```python
_SHELL_ALLOWED_BY_MODE: dict[SandboxMode, frozenset[Risk]] = {
    "read-only": frozenset({Risk.READ}),
    "workspace-write": frozenset({Risk.READ}),      # <- 注意这里
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
def judge_command(command, *, mode, policy, confined: bool = False) -> Verdict:
```

没想过这件事的调用方拿到的是十九章以来的行为，不是一个它没要求过的、
更安静的 agent。

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
@dataclass(frozen=True)
class Owner:
    key: str | None = None
    home: Path = MINICODEX_HOME

    def root(self) -> Path:
        if self.key is None:
            return self.home
        return self.home / TENANTS_DIR / self.key

    def memories(self) -> Path:
        return self.root() / "memories"
    def jobs_db(self) -> Path:
        return self.root() / "memory_jobs.sqlite3"
    def merge_lock(self) -> Path:
        return self.root() / "memory_merge.lock"
```

`key=None` 是单租户，是所有地方的默认值，而且**它必须精确等于第 16、17 章
已经在用的路径**。这不是希望，是断言：

```python
def test_F20_08_the_default_owner_is_todays_paths(tmp_path: Path) -> None:
    assert DEFAULT_OWNER.memories() == DEFAULT_MEMORY_DIR
    assert DEFAULT_OWNER.jobs_db() == DEFAULT_JOBS_PATH
    assert DEFAULT_OWNER.merge_lock() == MERGE_LOCK
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

### 9.4 这一章到此为止，下一章接着走

必须说清楚，否则会看起来像没做完：

**这是缝，不是多用户系统。** 这里没有注册、没有会话、没有密码、没有配额，
而且 `Owner.key` 被构造它的人**完全信任**。一个从未认证的请求头里取出
owner 的服务器，只是把泄漏挪了个地方，没有堵上。

认证和配额是第 22 章。这一章的任务是保证它们到的时候，**有地方放**。

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

```python
class FakeSandbox:
    def wrap(self, command: str, *, cwd: str) -> list[str]:
        self.calls.append((command, cwd))
        script = f"import sys; sys.stdout.write({self._stdout!r}); sys.exit({self._exit})"
        return [sys.executable, "-c", script]
```

加了三个测试之后，25/25 全部被抓住。

---

## §11 清点：这一章写了多少代码

| 文件 | 行数 | 说明 |
|---|---|---|
| `src/minicodex/sandbox.py` | 约 300 | 一半是注释，而注释里一半是测量结果 |
| `src/minicodex/tenancy.py` | 约 140 | 归属那条缝 |
| `src/minicodex/shell.py` | +30 | 一个分支，一个错误路径 |
| `src/minicodex/policy.py` | +40 | 第二张表和一个参数 |
| `tests/test_faults_ch20.py` | 约 420 | 48 个测试 |
| `probe_sandbox.py` | 约 260 | 六段，全部 Linux-only |
| `probe_mutations_ch20.py` | 约 250 | 26 条变异 |

`shell.py` 只改了两处，`policy.py` 只加了一个分支——这是刻意的。
沙箱是**加在接缝上**的，不是重写执行路径。1822 个测试里，
十九章写下的那些一个都没改。

---

## §12 验证

```
$ uv run ruff format . && uv run ruff check .
115 files left unchanged
All checks passed!

$ uv run pytest -q
1822 passed

$ uv run python probe_mutations_ch20.py
26 mutations, tests/test_faults_ch20.py tests/test_approval.py tests/test_shell.py
...
every mutation was caught.

$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone
```

在 WSL 里，那两个被跳过的测试真的会跑：

```
$ wsl -d Ubuntu -- bash -lc "... pytest tests/test_faults_ch20.py -q"
..............................................  [100%]
```

**46 passed，0 skipped。** 这一行是守卫在做事的证据——同一份测试，
在有 `bwrap` 的机器上一个都不跳。

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

### PR 描述

**What** — `run_shell` 现在可以在 bubblewrap 里跑，Linux only。
新增 `sandbox.py`（模式 → bwrap 命令行）和 `tenancy.py`（`Owner`）。
默认关闭。

**Why** — 第 5 章的 F05-04 和 F05-05 都是"识别了但拦不住"，
它们自己写着需要一个 OS 沙箱。这是那个沙箱。附带的收益是
`workspace-write` 终于可以名副其实，减掉一批 F05-06 量到的确认框。

**How** — `ShellSession(sandbox=...)`，默认 `None`。
`wrap()` 是纯函数，所以模块本身在任何平台都能测；
真正需要内核的测试用 `needs_bwrap` 守着，reason 写明缺的是 `bwrap` 而不是平台。

**Testing** — 1822 passed（Windows，2 skipped；WSL，0 skipped）。
25/25 变异被抓。`probe_sandbox.py` 六段实测。

**What this does not do** — macOS/Windows 后端、策略语言、按域名的网络规则、
认证和配额（第 22 章）。`setup_failure` 用前缀而不是 `--info-fd`，
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
   这是第 22 章的地基。

5. **复现第 19 章和第 20 章共有的那个故障。** 找一个你自己项目里
   "只在某个平台跑得起来"的测试，把它保护的机制删掉，看测试是不是还绿。
   如果是，你就找到了一个同样形状的洞。
