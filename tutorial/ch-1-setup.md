# 第 -1 章 · 把一个想法变成一个能装的东西

> **代码**：`steps/step-1_setup/`
> **分支**：`main`
> **产出**：一个别人能装上、能在终端里跑起来的 Python 包
> **前置**：会写 Python 基础（变量、`if`、`for`、列表、字典、函数）；能打开一个终端。
> 其余的——打包、虚拟环境、测试、git、CI——本章从零讲。

---

## §0 开工前的准备

### 0.1 装两个工具

本章只需要两个外部工具：

- **uv**：管理 Python 环境和依赖的工具。本章所有的"建环境""装包""打包"都用它。
- **git**：版本控制工具。§4 一开头就会用到。

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows（PowerShell）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

git 从 <https://git-scm.com> 下载安装。装完后在终端里分别敲 `uv --version` 和
`git --version`，都能打印出版本号就说明装好了。

### 0.2 怎么读本章的终端片段

```
$ uv --version
uv 0.12.1 (329541a50 2026-07-31 x86_64-pc-windows-msvc)
```

（这是我电脑上的输出，你的版本号会不同。）

- 以 `$` 开头的行是**你要敲的命令**（`$` 本身不用敲）。
- 没有 `$` 的行是命令的**输出**。
- `A && B` 的意思是"先跑 A，成功了再跑 B"。
- `A | B` 的意思是"把 A 的输出交给 B 处理"。

除特别说明外，本章的输出都是在 Linux 上真实跑出来的。**如果你用 Windows**，记住下面这张对照表，
其余命令都一样：

| 本章写的 | Windows 上对应的 |
|---|---|
| `python3` | `python` |
| `.venv/bin/python` | `.venv\Scripts\python.exe` |
| `.venv/bin/` 目录 | `.venv\Scripts\` 目录 |
| `/tmp` | 任意一个不是项目目录的文件夹，例如 `C:\temp` |
| `ls` | `dir`（PowerShell 里 `ls` 也能用） |

### 0.3 本章会遇到的新 Python 写法

你不需要提前学会它们，第一次出现时都会解释。先有个印象：

| 写法 | 意思 |
|---|---|
| `def main() -> int:` | `-> int` 是**类型标注**，说明这个函数返回整数。不写也能跑，写了方便人和工具读 |
| `list[str] \| None` | "一个字符串列表，或者 `None`"。同样只是标注 |
| `if __name__ == "__main__":` | "只有这个文件被直接运行时才执行下面的代码" |
| `Path(__file__).parent` | `__file__` 是当前文件的路径，`.parent` 是它所在的文件夹 |
| `try: ... except X: ...` | 尝试执行，如果出了 `X` 这种错误就走 `except` 分支 |
| `@something` 写在函数上一行 | **装饰器**，给函数附加额外行为。本章只在测试里用到 |

---

## §1 这一章要做出来的东西

一条命令：

```bash
minicodex --version
```

看起来毫无难度。但加两个约束之后，它就变成了一整章：

1. 这条命令要能在**一台没见过这个仓库的电脑**上跑起来
2. 装好 uv 之后，从零到跑起来，**不超过三条命令**

为什么是这个目标，而不是"先把 Agent 写出来"？

因为后面每一章都要回答同一个问题：**我改的这个东西，到底有没有真的生效？**
而回答这个问题的前提是——你的代码得先能被**装**到一个确定的地方，测试跑的得是
**那个**东西，而不是你硬盘上碰巧躺着的某个文件夹。

这件事不会自动成立。本章有一半篇幅是在证明它不会自动成立。

### 本书每一章都走的五步

这一章同时也是在演示全书的工作方法。每一章都按这个顺序走：

1. **定需求**：把一句话的目标，拆成一份具体的待办清单
2. **猜故障**：动工之前，先写下"我觉得它可能会坏在哪"，每条都要能验证
3. **做出来**：一段一段写，每一步都真的跑一遍
4. **逐条验证**：去撞每一条猜测。成立的修掉，并写测试钉住；不成立的也留下证据
5. **回顾**：猜对了几条，猜错了几条，**有哪些根本没猜到**

在这五步里，有三件事贯穿全书，后面称为**三条主线**：

- **主线 A · 需求变代码**：怎么把目标想清楚、拆开、写出来
- **主线 B · 工程化交付**：commit、分支、CI 这些"怎么交出去"的习惯
- **主线 C · 故障**：怎么预先猜到、验证、并用测试防住它们

---

## §2 定需求：把目标翻译成待办

方法很笨，但是有效：**把目标里的每个词拆开问"这需要什么"**，一直问到答案是一个
具体动作为止。

> "让**别人**在**新电脑**上，用**三条命令**，跑起来 `minicodex --version`"

拆开：

| 目标里的词 | 追问 | 需要做的事 |
|---|---|---|
| 别人 | 别人怎么拿到我的代码？ | 代码要能被**打包**成标准格式，并交给 **git** 管理 |
| 新电脑 | 他电脑上没有我的依赖 | 依赖要**声明**出来，让工具自动装 |
| 跑起来 | 装完之后凭什么能跑？ | 要**装到 Python 找得到的地方** |
| `minicodex` 这条命令 | 为什么敲这个词就有反应？ | 要注册一个**命令入口** |
| 三条命令 | 哪三条？ | 装依赖 / 跑命令 / 跑测试 |

这五行是从需求里直接推出来的。但光做完这五行还不够，还要再追问两个问题：

> **追问一：我怎么验证它真的成了？** 光看它打印出来不算。→ 要有**测试**
>
> **追问二：它明天、在别人那里还成吗？**
> - 明天：依赖升级了怎么办？→ 要**锁版本**
> - 别人那里：我只有一台电脑怎么办？→ 要有 **CI**（一台自动帮你验证的干净机器）

**这两个追问是本书最重要的思维习惯。** 大部分工程化的东西——测试、CI、锁文件、
类型标注——都是这两个问题的答案。它们不是"规范要求的"，而是需求本来就有、
只是没说出口的部分。

**先做哪个？** 从"不做就没法验证下一步"的那个开始。这里就是"能装"。

---

## §3 猜故障：动工前写下它可能坏在哪

还没写一行代码，先把担心写下来。每条猜测都要写清楚**怎么判断它成立**——否则就只是
焦虑，不是猜测。

| 编号 | 我担心的事 | 怎么判断它成立 |
|---|---|---|
| F-1-01 | 测试测的是硬盘上的源码文件夹，不是装上的包 | 把包卸载掉，测试还是绿的 |
| F-1-02 | 同一份代码，我电脑上装出来的依赖和 CI 上的不一样 | 两边装出的版本不同 |
| F-1-03 | API 密钥被不小心提交进仓库 | `git status` 里出现密钥文件 |
| F-1-05 | 第一天就配了很重的 CI，结果大家嫌慢，开始绕过它 | 有人红着叉也点合并 |

> **编号怎么读**：`F-1-01` 表示"第一次在第 -1 章登记的第 1 号故障"。它是一个**固定 ID**，
> 以后章节顺序再怎么调整，这个编号都不会变。测试函数名里的 `F_1_01` 指的就是它，
> 所有编号汇总在仓库根目录的 `FAULTS.md`。
>
> **为什么没有 F-1-04？** 最初还有一条"不知道模型实际收到了什么"。但这一章还没有模型，
> 没有"请求"这个东西，这条猜测根本没法验证，于是它被挪到了第一次接入模型的那一章。
> **需求还没出现的时候，就不要动手。**

写完这张表就开工。F-1-03 只能预防、没法事后补救，所以动工的第一步就处理它（§4.1）；
其余三条在 §5、§7、§8 逐条回来验证。

---

## §4 做出来

从一个空目录开始。每一步都真的跑一遍，报错原样贴出来。每完成一个能跑的小步，
就当场提交一次。

### 4.1 第一件事：建仓库，写 `.gitignore`

还没写代码，为什么先弄 git？因为接下来的每一步都会在目录里生成一些**不该提交**的东西：
`uv venv` 会生成 `.venv/`，安装会生成 `*.egg-info/`，运行 Python 会生成 `__pycache__/`。
如果等到第一次提交时才想起来，新手最常见的操作是 `git add .`——于是这些东西连同
可能存在的密钥文件，一起进了仓库。

> **git 最少需要知道的几条命令**：
> ```bash
> git init -b main          # 把当前目录变成一个 git 仓库，主分支叫 main（只做一次）
> git status                # 看哪些文件改了、哪些还没被 git 管
> git add <文件>            # 把改动放进"准备提交"的区域；git add . 表示当前目录下的全部改动
> git commit -m "说明"      # 把准备好的改动存成一个快照（commit）
> git log --oneline         # 看历史上所有的 commit
> ```

```bash
mkdir minicodex && cd minicodex
git init -b main
```

然后在这个目录里建一个叫 `.gitignore` 的文件。列在里面的文件和文件夹，git 会假装没看见：

```gitignore
# Python build and cache artefacts
__pycache__/
*.py[cod]
*.egg-info/
.pytest_cache/
.ruff_cache/
dist/
build/

# Virtual environments -- rebuildable from uv.lock, never committed
.venv/
venv/

# Secrets.  A key that reaches git history is in git history: `git rm` does not
# remove it from past commits, forks, or anyone's existing clone.
.env
.env.*
*.key
*.pem
```

两类东西：**能重建的**（缓存、虚拟环境、构建产物），和**绝不能进去的**（密钥）。

第一类进了仓库只是脏，删掉就好。第二类就是 §3 的 F-1-03，它有个性质值得记住：
**它没有修复方案，只有止损方案。** 密钥一旦进了 git 历史，`git rm` 没用——历史还在，
别人的 clone 还在，fork 还在。正确流程是作废那个密钥、重写历史、通知所有人重新 clone。

所以只能预防，而预防的时机就是现在：第一个 commit 之前。如果不防，它通常以收到
GitHub 的密钥泄露告警邮件的方式暴露——那时已经晚了。

> **一个随时可用的习惯**：每次 `git add .` 之前先敲 `git status`，看一眼列出来的文件里
> 有没有不认识的。`.gitignore` 挡的是你想得到的东西，`git status` 抓的是你没想到的。

等有了测试工具，§7.6 会补一个测试，检查 `.gitignore` 里一直有 `.env`、`*.key` 这些条目，免得哪天被人删掉。

### 4.2 先写个能跑的东西

在项目根目录里再建一个同名文件夹，放两个文件：

```
minicodex/              ← 项目文件夹（后面叫它"项目根目录"）
├── .gitignore
└── minicodex/          ← Python 包
    ├── __init__.py
    └── __main__.py
```

> **包是什么**：一个装着 `__init__.py` 的文件夹，Python 就把它当成一个可以 `import` 的
> **包**。`__init__.py` 在 `import minicodex` 时执行。
> `__main__.py` 是特殊文件名：执行 `python -m minicodex` 时，Python 跑的就是它。

```python
# minicodex/__init__.py
__version__ = "0.0.1"
```

```python
# minicodex/__main__.py
from minicodex import __version__


def main() -> int:
    print(f"minicodex {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> **最后两行在干什么**：`main()` 返回 `0`，`raise SystemExit(0)` 让程序以"退出码 0"结束。
> 退出码是程序告诉终端"我成功了没有"的方式：0 表示成功，非 0 表示失败。
> CI 就是靠退出码判断红还是绿的。

在项目根目录里跑：

```
$ python3 -m minicodex
minicodex 0.0.1
```

成了。**但只在这个目录里成。** 换个地方：

```
$ cd /tmp && python3 -m minicodex
/usr/bin/python3: No module named minicodex
```

这个报错值得停下来看清楚，本章后面的所有问题都从它开始。

### 4.3 插播：`import` 到底在干什么

Python 执行 `import minicodex` 时，会按顺序翻一个列表——`sys.path`——看每个目录下
有没有叫这个名字的东西。

```
$ python3 -c "import sys; print(repr(sys.path[0]))"
''
```

> `python3 -c "..."` 的意思是"直接执行引号里的这行 Python 代码"。

第一项是 `''`，代表**当前工作目录**，也就是你执行命令时所在的文件夹。

所以 `python3 -m minicodex` 在项目根目录里能跑，纯粹是因为**当前目录里正好有个叫
`minicodex` 的文件夹**。这和"这个包被正确安装了"没有任何关系。

那"安装"是什么？**安装就是把你的代码复制（或者链接）到一个已经在 `sys.path` 上的
目录里**，这个目录通常叫 `site-packages`。

一句话记住：

> **`import` 找得到 ≠ 已安装。当前目录会白送你一次 import。**

这就是 §3 里 F-1-01 担心的事情的根源。§5 会回来验证它。

### 4.4 虚拟环境：给项目一个自己的 Python

要"安装"，先得有个装的地方。直接装到系统的 Python 里是个坏主意：

> **不用虚拟环境会怎样**：项目 A 要 `httpx 0.27`，项目 B 要 `0.28`，系统里只能装一个，
> 于是你在两个项目之间反复重装。这个坑每个 Python 开发者都踩过一次。

**虚拟环境**是一个独立的、只属于这个项目的 Python。装进去的包不会影响系统，也不会
被别的项目影响。在项目根目录里：

```
$ uv venv
Using CPython 3.10.12
Creating virtual environment at: .venv
Activate with: source .venv/bin/activate
```

> 最后一行提示的"激活"是可选的，本章不需要它：我们要么写出虚拟环境里 Python 的完整路径，
> 要么用 `uv run`。

这会生成一个 `.venv/` 文件夹。里面的 `.venv/bin/python`（Windows 上是
`.venv\Scripts\python.exe`）就是这个项目专属的 Python。后面 `uv` 装的东西都会进这里。

`.venv/` 可以随时删掉重建，它不属于源代码，§4.1 的 `.gitignore` 已经把它排除在 git 之外。

### 4.5 最小的 `pyproject.toml`

现在试着把包装进虚拟环境：

```
$ uv pip install -e .
error: /tmp/lab/step1 does not appear to be a Python project,
as neither `pyproject.toml` nor `setup.py` are present in the directory
```

> `/tmp/lab/step1` 是我演示时项目根目录的实际路径，你的会不同。
> 末尾的 `.` 表示"当前目录"。

工具说：**我不知道这是个什么东西。** 它需要一份说明书，文件名固定叫 `pyproject.toml`，
放在项目根目录。

> **`pyproject.toml` 是什么**：Python 官方规定的项目说明书，用 TOML 格式写（比 JSON 多了
> 注释，比 YAML 少了缩进陷阱）。pip、uv、poetry、hatch 这些工具读的都是同一份。
> 格式很简单：`[xxx]` 开一个段落，段落里一行一个 `键 = 值`。

先写能让它跑起来的**最少内容**：

```toml
[project]
name = "minicodex"
version = "0.0.1"
```

> `[project]` 这个段名是标准（PEP 621）规定的，工具只认这个名字。
> `name` 是别人 `pip install` 时敲的名字，`version` 是版本号。只有这两项必填。

再装一次：

```
$ uv pip install -e .
Resolved 1 package in 8ms
   Building minicodex @ file:///tmp/lab/step1
      Built minicodex @ file:///tmp/lab/step1
Prepared 1 package in 6.70s
Installed 1 package in 2ms
 + minicodex==0.0.1 (from file:///tmp/lab/step1)
```

装上了。换个目录，用虚拟环境里的 Python 验证一下：

```
$ cd /tmp && /tmp/lab/step1/.venv/bin/python -m minicodex
minicodex 0.0.1
```

**换目录也能跑了。** 三行 TOML 换来的。

> **`-e` 是什么**：可编辑安装（editable install）。它不复制代码，只在 `site-packages`
> 里放一个"指针"，指回你的源码目录。好处是**改了代码立刻生效，不用重装**，所以开发时
> 一般都用 `-e`。但请记住它的原理——它读的始终是你的源码目录。§6 会用到这一点。

### 4.6 变成一条命令

目标是敲 `minicodex`，不是敲 `python -m minicodex`。现在有这个命令吗？

```
$ ls .venv/bin/
activate  activate.bat  activate.csh  ...  python  python3  python3.10

$ minicodex
bash: minicodex: command not found
```

没有。因为**我没告诉任何人"我想要一条叫这个名字的命令"**。在 `pyproject.toml` 里加上：

```toml
[project.scripts]
minicodex = "minicodex.__main__:main"
```

> **怎么读这一行**：等号左边是要生成的命令名。右边是 `模块路径:函数名`——
> "去 `minicodex.__main__` 这个模块里，找一个叫 `main` 的函数，调用它"。
> 安装时，工具会在 `.venv/bin/` 下生成一个小程序干这件事。
> 这个机制叫 **entry point（入口点）**。

重新执行一次 `uv pip install -e .`，再看：

```
$ ls .venv/bin/ | grep minicodex
minicodex

$ cd /tmp && /tmp/lab/step1/.venv/bin/minicodex
minicodex 0.0.1
```

**§1 的目标达成了。** 一共五行 TOML。

> **为什么每次都要写 `.venv/bin/...` 的完整路径？** 因为 `.venv/bin/` 不在终端的命令
> 搜索路径里。以后我们会用 `uv run minicodex`，它会自动用这个项目的虚拟环境，
> 不用写路径。

### 4.7 顺手补上 `--version` 该有的样子

把 `__main__.py` 改成完整版：

```python
# minicodex/__main__.py
"""Command line entry point."""

from __future__ import annotations

import argparse
import platform
import sys

from minicodex import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minicodex")
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version and enough environment detail to file a bug report",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(f"minicodex {__version__}")
        print(f"python    {platform.python_version()} ({sys.platform})")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> **几个新东西**：
> - `argparse` 是标准库里解析命令行参数的模块。`add_argument("--version", action="store_true")`
>   的意思是：命令行里出现了 `--version`，`args.version` 就是 `True`，否则是 `False`。
> - `platform.python_version()` 返回 Python 版本号，`sys.platform` 返回操作系统名。
> - `from __future__ import annotations` 让较老的 Python 也能看懂 `list[str] | None`
>   这种类型标注写法。

为什么要打印 Python 版本和平台？

因为一年后你会收到这样的 issue：**"跑不了"**。没有版本号，没有系统信息，没有复现步骤。
`--version` 多打两行，就等于给了对方一条"把这个贴上来"的指令。**现在花两分钟，
以后省两小时。**

`main(argv=None)` 这个参数也不是摆设：测试可以直接调用 `main(["--version"])`，不用
去模拟真实的命令行。**一个函数好不好测试，往往取决于它有没有一个能从外面传东西
进去的入口。**

### 4.8 补全项目信息，写一份 README

§4.5 的 `[project]` 只有必填的两项。在第一次提交之前，再补三项：

```toml
[project]
name = "minicodex"
version = "0.0.1"
description = "A Codex-like coding agent, built from scratch, one failure at a time."
readme = "README.md"
requires-python = ">=3.10"
```

> - **`description`**：一句话介绍，会显示在包的索引页面上。
> - **`readme`**：指向一个说明文件，它的内容会成为包的长介绍。**这个文件必须存在**，
>   否则打包会直接失败。
> - **`requires-python`**：支持的最低 Python 版本。用户的 Python 比它旧时，pip 会拒绝安装，
>   而不是装上之后在某行代码上莫名其妙地报错。写 3.10，是因为本项目只在 3.10 及以上
>   测试过，没测过的版本就不承诺。

然后在项目根目录建 `README.md`，写清楚这是什么、怎么跑：

````markdown
# minicodex — step -1: a package that installs

No agent yet. A package that installs, runs, tests and ships.

```bash
uv sync --all-extras
uv run minicodex --version
uv run pytest
```
````

> 这三条命令现在还跑不通，要到 §7 声明完依赖才行。README 写的是这一章最终要达到的状态，
> 也就是 §1 说的"三条命令"。

改了 `pyproject.toml` 以后，重新执行一次 `uv pip install -e .`，让安装信息跟着更新。

### 4.9 第一次提交

包能装、命令能跑。这就是一个"能跑的小步"，现在提交。**不是"等功能写完了再提交"**：
commit 的价值是"一个随时可以回到的点"，一个能跑的骨架就是一个很有用的回退点。

```bash
git status          # 先看一眼：.venv/、__pycache__/、*.egg-info/ 都不该出现在列表里
git add .
git commit          # 不带 -m，git 会打开一个编辑器让你写 message
```

在编辑器里写下这些，保存并关闭，提交就完成了：

```
chore: make minicodex installable as a package

Goal for this step: `minicodex --version` works on a machine that has never
seen this repository, in three commands.

The command comes from a [project.scripts] entry point, so it exists
wherever the package is installed, not only inside this directory.
```

> **commit message 的格式**：第一行是**标题**，写"做了什么"，格式是 `类型: 一句话`。
> 常用类型：`feat`（新功能）、`fix`（修 bug）、`test`（测试）、`refactor`（重构，不改行为）、
> `chore`（杂务）、`ci`（CI 配置）、`docs`（文档）。
>
> 标题下面空一行是**正文**，写"为什么"。只有一行标题时，用 `git commit -m "标题"` 更快。
>
> 为什么这么讲究，§9 回头统一说。

本章直接提交在 `main` 分支上，原因也在 §9 说。

---

## §5 验证 F-1-01：测试测的真是装上的包吗

`minicodex --version` 能跑了。现在进入追问一：**我怎么验证？** 先写一个测试，然后去
撞 §3 里的第一条猜测。

### 5.1 装测试工具，写第一个测试

**pytest** 是 Python 最常用的测试工具。它会自动找 `tests/` 目录下所有 `test_` 开头的
函数，逐个运行；函数里的 `assert` 条件为假，这个测试就算失败。

```
$ uv pip install pytest
```

在项目根目录建 `tests/test_smoke.py`：

```python
# tests/test_smoke.py
from minicodex import __version__


def test_version_is_set() -> None:
    assert __version__
```

> `assert __version__` 的意思是"断言 `__version__` 不是空的"。

```
$ .venv/bin/python -m pytest -q
.                                                                        [100%]
1 passed in 0.00s
```

> `-q` 让输出更简洁。每个 `.` 代表一个通过的测试。

绿了。

### 5.2 现在把包卸载掉

照 §3 写的判断方法：卸载掉，看测试还绿不绿。

```
$ uv pip uninstall minicodex
Uninstalled 1 package in 1ms
 - minicodex==0.0.1

$ .venv/bin/python -c "import importlib.metadata as m; print(m.version('minicodex'))"
importlib.metadata.PackageNotFoundError: No package metadata was found for minicodex
```

> `importlib.metadata.version()` 是去查"已安装包的登记信息"。它报错，说明包确实没了。

再跑测试：

```
$ .venv/bin/python -m pytest -q
.                                                                        [100%]
1 passed in 0.00s
```

**包被卸载了，测试依然全绿。** 看看它 import 到的是什么：

```
$ .venv/bin/python -c "import minicodex; print(minicodex.__file__)"
/tmp/lab/step1/minicodex/__init__.py
```

源码目录。就是 §4.3 说的那件事：**当前目录白送的那次 import**。`python -m pytest`
和 `python -m minicodex` 一样，会把当前目录放进 `sys.path`。

**F-1-01 成立。** 这个测试从来没有测过"安装"这件事，它测的只是"我硬盘上有这个文件夹"。

### 5.3 修：把包挪进 `src/`

```
minicodex/              ← 项目根目录
├── src/
│   └── minicodex/
│       ├── __init__.py
│       └── __main__.py
├── tests/
│   └── test_smoke.py
└── pyproject.toml
```

就是把包挪进一个叫 `src/` 的文件夹里，这种结构叫 **src layout**。为什么这一挪就能解决
问题？

因为现在项目根目录里**没有**叫 `minicodex` 的文件夹了，当前目录那次白送的 import
送不出去了。不安装，就 import 不到：

```
$ .venv/bin/python -m pytest -q                # 故意不装
tests/test_smoke.py:1: in <module>
    from minicodex import __version__
E   ModuleNotFoundError: No module named 'minicodex'
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.08s
```

**从"静默地绿"变成了"响亮地红"。** 这次改动换来的就是这一点，而这已经足够值了。

> **一条可以带走的原则**：把一个悄悄出错的场景，改造成一个大声报错的场景。
> 这比"修好这个 bug"更有价值——因为你修的是**一类** bug 的可见性，
> 而不是一个 bug 的实例。本书会反复用这一招。

`pyproject.toml` 不用改：打包工具能自动认出 `src/` 这种结构。重新 `uv pip install -e .`，
测试又绿了——这次是因为包真的装上了。

> 这里的"自动认出"是谁做的，§6 会发现这是个隐患。

### 5.4 把这次验证写成测试

修好不够。**下次有人改了目录结构，怎么保证不会再犯？** 答案是把刚才手动做的那几次
检查，写成会自动替你检查的测试。

`test_smoke.py` 可以删掉了。它只检查了 `__version__` 不为空，而刚才的实验证明：
这种测试在包没装上时照样是绿的。**它测的东西，下面的新测试都覆盖了，而且覆盖得更严。**

直接删掉 `tests/test_smoke.py` 这个文件。它从来没提交过，所以 git 那边什么都不用做。

新建 `tests/test_packaging.py`。这个文件会贯穿整章，每验证一条猜测就往里面加测试。
现在它是这样的：

```python
# tests/test_packaging.py
"""What we learned about packaging, written down so it stays true.

Every test here corresponds to something that broke, or would have broken
silently, while building this chapter.  Fault IDs match FAULTS.md.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

import minicodex


def test_F_1_01_package_is_installed_not_merely_importable() -> None:
    """Under a flat layout `import minicodex` succeeds from the repo root even
    when nothing was installed, because '' is on sys.path.  Asking for the
    metadata is a question only a real installation can answer.
    """
    try:
        assert version("minicodex")
    except PackageNotFoundError:  # pragma: no cover - only when misconfigured
        pytest.fail("minicodex is not installed; run `uv sync --all-extras`")


def test_F_1_01_import_comes_from_src_not_from_the_working_directory() -> None:
    assert Path(minicodex.__file__).resolve().parent.parent.name == "src"


def test_console_script_runs_outside_the_project_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "minicodex", "--version"],
        cwd=tmp_path,  # deliberately not the repo: cwd must not be load-bearing
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("minicodex ")
    assert "python" in result.stdout
```

三个测试，对应 §5.2 手动做过的三件事：

> **第一个：包真的装上了吗？** 用 `importlib.metadata.version()`，而不是 `import minicodex`——
> 因为**登记信息是源码目录给不出来的东西**，只有真正安装过的包才有。这是区分"已安装"和
> "文件恰好在那儿"最可靠的办法。
> - `try ... except PackageNotFoundError`：查不到登记信息时会抛这个错误，接住它，
>   换成 `pytest.fail(...)`。后者让测试失败，并打印一句**告诉看到红色的人该怎么修**的提示。
> - `# pragma: no cover` 是写给测试覆盖率工具看的注释，意思是"这行正常情况下不会执行，
>   统计时别算它"。现在用不上，可以忽略。
>
> **第二个：import 到的是 `src/` 里的那份吗？** 相当于 §5.2 那条打印 `minicodex.__file__`
> 的命令。`Path(...).resolve()` 把路径变成完整的绝对路径；`__init__.py` 往上两层
> （`.parent.parent`）应该是一个叫 `src` 的文件夹。如果有人把包挪回项目根目录，
> 这个测试就会红。
>
> **第三个：换个目录，命令还能跑吗？** 这就是 §1 的目标本身。
> - `subprocess.run([...])` 在 Python 里执行一条终端命令。
> - `sys.executable` 是**当前正在运行的这个 Python** 的路径，也就是虚拟环境里的那个。
>   用它而不是写死 `"python"`，保证子进程和测试用的是同一个环境。
> - `cwd=tmp_path`：让这条命令在一个临时文件夹里执行，**故意**不在项目目录里，
>   这样当前目录就白送不了 import。`tmp_path` 是 pytest 自动提供的临时文件夹，§6.5 细讲。
> - `capture_output=True, text=True`：把命令的输出收集起来，并当作文本（而不是字节）返回，
>   后面才能用 `result.stdout` 检查。`check=True`：命令失败就直接报错。

既然要正式写测试了，顺手在 `pyproject.toml` 里给 pytest 加上配置：

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
# --strict-markers: a typo like @pytest.mark.slwo becomes an error instead of
# a decorator that silently does nothing.
addopts = "-q --strict-markers"
```

> - **`testpaths`**：只去 `tests/` 目录里找测试，不去翻 `.venv/` 之类的地方。
> - **`addopts`**：每次运行 pytest 都自动加上的参数。有了 `-q`，以后就不用每次手敲了。
> - **`--strict-markers`**：测试上会贴 `@pytest.mark.xxx` 这样的标记（§6.5 会见到一个）。
>   标记名拼错时，pytest 默认只会悄悄忽略它；加上这个参数，拼错就直接报错。
>   **又是一次"把悄悄出错改成大声报错"。**

### 5.5 提交

挪目录、换测试、加 pytest 配置，看起来是几件事，但它们服务于**同一个想法**：
让测试只能跑在真正装上的包上。所以它们放在一个 commit 里。

```bash
git status          # 应该看到 minicodex/ 被挪到 src/minicodex/、pyproject.toml 改了、
                    # tests/ 新增
git add .
git commit
```

```
fix: make tests run against the installed package, not the source tree

Found by uninstalling the package: the tests stayed green, because a flat
layout puts the repo root on sys.path and `import minicodex` succeeds
whether or not anything was installed.

Moved the package under src/, so an uninstalled package is now a
collection error instead of a silent pass. The smoke test is replaced by
tests that ask for the package metadata, check the import comes from src/,
and run the command from a directory that is not the repository.
```
> 注意正文的第一段：**这个问题是怎么发现的。** 半年后有人问"为什么非要放在 `src/` 底下，
> 我能挪出来吗？"，答案就在这里。

---

## §6 意外：可编辑安装藏住的 bug

F-1-01 按预期被撞到、修好了。但接下来发生的这件事，§3 的猜测表里**没有**。

### 6.1 加一个数据文件

给包加一个非 Python 文件——一个提示词模板。Agent 项目里迟早会有一堆这样的文件：

```
src/minicodex/
├── __init__.py
├── __main__.py
└── prompts/
    └── system.md       ← 内容：You are a coding agent working in a user's repository.
```

把 `__init__.py` 改成下面这样，加一个读它的函数：

```python
# src/minicodex/__init__.py
"""minicodex -- a Codex-like coding agent, built from scratch.

There is no agent yet.  There is a package that installs, runs, tests and
ships.  Everything after this chapter is built on top of it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__", "system_prompt"]

__version__ = "0.0.1"

_PROMPTS = Path(__file__).parent / "prompts"


def system_prompt() -> str:
    """Read the agent's system prompt from a file shipped inside the package.

    Nothing uses this yet.  It exists because a data file is the cheapest way
    to prove that packaging works: code that only imports .py files will pass
    every test even when the wheel is broken.
    """
    return (_PROMPTS / "system.md").read_text(encoding="utf-8")
```

> - **开头的三引号字符串**叫 **docstring**，是写给人看的说明。放在文件开头就是整个模块的说明，
>   放在函数第一行就是这个函数的说明。
> - **`__all__`** 是显式的导出清单：别人写 `from minicodex import *` 时，只会拿到这里列出的
>   名字。它同时也告诉读代码的人：这个包对外提供的就是这两样东西。
> - **`_PROMPTS = Path(__file__).parent / "prompts"`**：`Path(__file__).parent` 是
>   `__init__.py` 所在的文件夹，`Path` 对象可以用 `/` 拼接路径，所以 `_PROMPTS` 指向
>   "包文件夹下的 `prompts/`"。用它而不是 `Path("prompts")`，是为了**不管从哪个目录运行，
>   都能找到文件**——后者是相对于当前工作目录的，换个目录就找不到了。
>   名字前面的下划线是 Python 的约定，表示"内部使用，外面别碰"。
> - `read_text(encoding="utf-8")`：把文件读成字符串。**明确写出编码**，否则在某些系统上
>   （尤其是中文 Windows）会用别的编码去读，遇到非英文字符就出错。

这个函数目前没有任何代码调用。docstring 写明了它存在的理由：**数据文件是证明打包正常的
最便宜的办法。** 只包含 `.py` 文件的包，就算打包坏了，测试也照样能过。

在 `README.md` 末尾也补一句，免得别人以为这是个没用的文件、顺手删掉：

```markdown
`src/minicodex/prompts/system.md` is unused by any code path. It is here so
that a broken wheel fails a test instead of failing a user.
```

在 `tests/test_packaging.py` 末尾追加一个测试：

```python
def test_F_1_01_data_files_are_readable_at_runtime() -> None:
    """Code-only tests pass against a broken wheel: the .py files are always
    there.  Reading a packaged file is what makes packaging observable at all.
    """
    assert "coding agent" in minicodex.system_prompt()
```

```
$ .venv/bin/python -m pytest
....                                                                     [100%]
4 passed in 0.11s
```

（有了 §5.4 的 pytest 配置，不用再手敲 `-q` 了。）

绿。

### 6.2 打开用户真正拿到的东西看看

用户 `pip install` 拿到的不是你的文件夹，是一个打包好的文件：

> **wheel 是什么**：`.whl` 文件，Python 包的标准分发格式，本质上是一个 zip 压缩包。
> **它就是用户实际收到的东西。**

打一个 wheel 出来，打开看看里面有什么：

```
$ uv build --wheel
$ .venv/bin/python -c "import zipfile; [print(' ', n) for n in zipfile.ZipFile('dist/minicodex-0.0.1-py3-none-any.whl').namelist()]"
  minicodex/__init__.py
  minicodex/__main__.py
  minicodex-0.0.1.dist-info/METADATA
  minicodex-0.0.1.dist-info/WHEEL
  minicodex-0.0.1.dist-info/entry_points.txt
  minicodex-0.0.1.dist-info/top_level.txt
  minicodex-0.0.1.dist-info/RECORD
```

> 第二条命令是"用 `zipfile` 打开 wheel，把里面的文件名一个个打印出来"。
> 这里把 `for` 循环塞进了一个列表推导式，只为了写成一行；平时别这么写。

**`system.md` 不在里面。** 模拟一个用户，在一个全新的虚拟环境里装这个 wheel：

```
$ uv venv /tmp/user && uv pip install --python /tmp/user/bin/python dist/minicodex-0.0.1-py3-none-any.whl
$ /tmp/user/bin/python -c "from minicodex import system_prompt; print(system_prompt())"

FileNotFoundError: [Errno 2] No such file or directory:
'/tmp/user/lib/python3.10/site-packages/minicodex/prompts/system.md'
```

**测试全绿，用户装上就崩。** 这是最糟的一种故障。

### 6.3 为什么测试没抓到

完整的因果链：

1. `pyproject.toml` 里没有声明用谁来打包，工具就**悄悄回退到 setuptools**。§5.3 说的
   "打包工具能自动认出 `src/`"，就是它在认。而在这种配置下，setuptools 默认
   **不把非 `.py` 文件放进 wheel**
2. 我的测试跑在可编辑安装上，**读的是源码目录**，文件当然在
3. 于是测试永远是绿的
4. 用户装上，崩

**注意第 2 步。** 我在 §4.5 说过"开发时一般都用 `-e`"，而正是 `-e` 让这个 bug 隐身了。
工具没有骗你，是你问的问题不对：你问的是"源码能不能用"，而用户关心的是
"wheel 能不能用"。

这也是为什么 §3 没猜到它：F-1-01 猜的是"import 来自源码目录"，src layout 修的也只是
这个。但**即使包真的装上了**，可编辑安装读的仍然是源码目录——它对打包问题完全免疫。

### 6.4 修：显式声明构建后端

不再让工具自己猜，在 `pyproject.toml` 里明确写出用谁来打包：

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/minicodex"]
```

> **`[build-system]` 是什么**：告诉工具"用谁把我这个目录变成可安装的包"。
> 这个角色叫**构建后端（build backend）**。`requires` 是它自己需要装什么，
> `build-backend` 是从哪个模块调用它。这里选的是 **hatchling**。
>
> `packages = ["src/minicodex"]` 其实可以不写，hatchling 能自己找到。写出来的理由是：
> **靠自动推断的构建，会在你给某个目录改名时悄悄换一种行为。** 写明白只需要一行。
>
> **为什么选 hatchling 而不是 setuptools**：hatchling 的默认行为符合直觉——包目录下的
> 所有文件都进 wheel，包括 `.md`。setuptools 出于历史原因，要额外配置才会收非 `.py`
> 文件。一个"默认就对"的工具，价值远大于"配置完也能对"的工具。
>
> **不写 `[build-system]` 还有一层问题**：不同工具回退的行为不完全一致。也就是说，
> **你打出来的东西取决于是谁打的。** 所以它同时也在防 F-1-02。

现在再打开 wheel：

```
   minicodex/__init__.py
   minicodex/__main__.py
   minicodex/prompts/system.md          ← 在里面了
   minicodex-0.0.1.dist-info/METADATA
   minicodex-0.0.1.dist-info/WHEEL
   minicodex-0.0.1.dist-info/entry_points.txt
   minicodex-0.0.1.dist-info/RECORD
```

### 6.5 把这次教训写成测试

§6.1 那个 `test_F_1_01_data_files_are_readable_at_runtime` 在坏掉的配置下也是绿的，
它抓不住这个 bug。真正能抓住它的只有一个办法：**把 wheel 打出来，打开看。**

先在 `tests/test_packaging.py` 顶部的 import 里补上两个标准库模块：

```python
import shutil
import zipfile
```

然后在文件末尾追加：

```python
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to build a wheel")
def test_F_1_01_built_wheel_actually_contains_the_data_file(
    repo_root: Path, tmp_path: Path
) -> None:
    """The test above is not enough, and finding that out cost an afternoon.

    An editable install puts the *source tree* on the path, so `system_prompt()`
    reads the file straight off disk and passes no matter what the wheel
    contains.  The only way to know what users receive is to build the artefact
    and look inside it.  Takes about a quarter of a second.
    """
    subprocess.run(
        ["uv", "build", "--wheel", "-o", str(tmp_path), str(repo_root)],
        check=True,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()

    assert "minicodex/prompts/system.md" in names, (
        f"the wheel users install is missing the prompt file; it contains {names}"
    )
```

逐行拆开：

> - `@pytest.mark.skipif(条件, reason=...)`：一个装饰器，条件成立时跳过这个测试，
>   而不是让它莫名其妙地失败。`shutil.which("uv")` 在终端的命令搜索路径里找 `uv`，
>   找不到就返回 `None`。
> - `repo_root` 和 `tmp_path` 这两个参数是 pytest 自动传进来的，叫 **fixture**。
>   `tmp_path` 是 pytest 内置的，每个测试都会拿到一个新的临时文件夹；`repo_root` 是我们在
>   `tests/conftest.py` 里自己定义的，值是项目根目录的路径（代码见下）。
> - `subprocess.run([...])`：在 Python 里执行一条终端命令，这里是 `uv build --wheel`。
>   `check=True` 表示命令失败就直接报错。
> - `next(tmp_path.glob("*.whl"))`：在临时文件夹里找第一个 `.whl` 文件。
> - `zipfile.ZipFile(wheel).namelist()`：wheel 就是 zip，打开它，列出里面所有文件的路径。
> - 最后的 `assert` 带了一句说明，失败时会打印出 wheel 里实际有什么，方便排查。

`conftest.py` 是 pytest 的特殊文件名，里面定义的 fixture 所有测试都能用：

```python
# tests/conftest.py
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
```

> - `@pytest.fixture`：把下面的函数登记成一个 fixture。测试函数的参数里写了 `repo_root`，
>   pytest 就会调用这个函数，把返回值传进去。
> - `Path(__file__).resolve().parent.parent`：`conftest.py` 所在的是 `tests/`，
>   再往上一层就是项目根目录。

这个测试 0.31 秒，是整个测试文件里**唯一**能抓住这个 bug 的。

### 6.6 验证测试真的有用

一个绿色的测试不能说明它有用——它可能对着坏代码也一样绿（§6.1 那个读数据文件的测试
就是）。所以：**把修复改回去，看它红不红。**

把 `[build-system]` 换回 setuptools、去掉 `packages` 配置，跑测试。下面的输出是本章
全部写完之后做的这次反向验证，所以除了刚写的测试，还多红了一个 §7 才加的测试：

```
FAILED tests/test_packaging.py::test_F_1_01_built_wheel_actually_contains_the_data_file
FAILED tests/test_packaging.py::test_F_1_02_build_backend_is_declared
```

红了。第二个失败的测试检查的是 `[build-system]` 有没有被声明，§7 会讲它。
把修复改回来，全绿。

> **一条可以带走的规则**：写完一个修复，花三十秒把修复改回去，确认对应的测试会红。
> **一个对着坏代码也能通过的测试，不是回归测试，是装饰品。**
> 这是性价比最高的一个习惯，全书每一章都会做。

### 6.7 提交

```bash
git status          # 应该看到 pyproject.toml、src/minicodex/ 和 tests/ 下的改动
git add .
git commit
```

```
fix: ship the prompt file inside the wheel

Found while writing the packaging tests: the suite was green, the editable
install worked, and the built wheel contained no .md files at all. An
editable install puts the source tree on sys.path, so every test read the
file straight off disk and never touched the artefact users receive.

Two changes. hatchling with an explicit `packages` entry, because
setuptools excludes non-Python files from a wheel unless told otherwise.
And a test that builds the wheel and looks inside it -- 0.3s, and the only
test in this file that could have caught the bug.
```

> 这条 message 最有价值的是第一段：**它写清了这个 bug 是怎么被发现的。**
> 半年后有人嫌那个 0.3 秒的测试慢、想删掉时，这段话就是保留它的理由。

---

## §7 验证 F-1-02：明天、在别人那里还能装出一样的东西吗

进入追问二的前一半："明天还成吗"。

先说清楚：**这一条本章没有真的撞上。** 要撞上它，得等某个依赖真的发布了不兼容的新版本。
所以这里做的是**预防**，测试钉住的是"预防措施还在不在"，而不是"故障会不会发生"。
如果不防，它通常以 CI 突然变红、本地却一切正常的方式暴露出来。

### 7.1 依赖分两类

```toml
[project]
dependencies = []

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "ruff>=0.6",
    "pyyaml>=6.0",
    "tomli>=2.0; python_version < '3.11'",
]
```

> **`dependencies`** 是用户装你的包时会被一起装上的东西。
> **`optional-dependencies`** 是按需安装的，`dev` 这一组只有开发者才需要。
>
> 分开的理由很实在：**用户不需要你的测试工具。** 混在一起，每个用户都得下载 pytest。

`dev` 里的四个包各有用处：

| 包 | 用来干什么 |
|---|---|
| `pytest` | 跑测试，§5 起一直在用 |
| `ruff` | 格式化和检查代码，§8 会配置它 |
| `pyyaml` | 读 YAML 文件。§8 的测试要读 CI 配置文件（它是 YAML 格式），检查它写得对不对 |
| `tomli` | 读 TOML 文件。本节的测试要读 `pyproject.toml`，只在旧版 Python 上需要，见下文 |

注意 `dependencies = []`——**运行时依赖是空的**。本章只用标准库，直到要通过网络和模型
通信时，才会加第一个依赖。

每加一个依赖都是一次决策：多一个可能有漏洞的东西、多一个可能不兼容的东西、多一个
可能停止维护的东西。**能不加就不加。**

最后那行值得单独说：

```toml
"tomli>=2.0; python_version < '3.11'",
```

> 分号后面是**环境标记（environment marker）**：只在满足条件时才装。
> `tomllib` 从 Python 3.11 起才进入标准库，旧版本需要 `tomli` 这个向后移植（backport）包。
> 有了标记，**一份依赖列表能同时对所有 Python 版本都正确**。

有了这些声明，以后装环境就不用再一个个 `uv pip install` 了，一条命令搞定：

```bash
uv sync --all-extras      # 建虚拟环境 + 装本项目 + 装所有依赖（含 dev）
```

### 7.2 版本号为什么都带 `>=`

```
"pytest>=8.0"
```

不写版本会怎样？工具会装当时能找到的最新版。今天是 pytest 9，三个月后可能是 10，
某个行为变了，**同一份代码在两天之间有了两种结果**。

`>=8.0` 是**下界**：低于这个版本我保证不行。但它不解决"今天和明天装的不一样"。

### 7.3 锁文件

```bash
uv lock
```

生成 `uv.lock`，**把它提交进仓库**。

> **锁文件**记录的是**这一次解析出来的每个包的确切版本和校验值**。
> `pyproject.toml` 说的是"我能接受什么范围"，锁文件说的是"这次实际装了什么"。

CI 里这样用：

```yaml
run: uv sync --frozen --all-extras
```

> `--frozen`：如果 `uv.lock` 和 `pyproject.toml` 对不上，**直接失败**，
> 不许自作主张重新解析。这样"本地和 CI 装的不一样"就从玄学变成一个红叉。

> **常见困惑**：发布给别人用的库，不是不该锁死版本吗？
> 两回事。`pyproject.toml` 里的 `>=8.0` 是**给用户的兼容承诺**；
> `uv.lock` 是**给贡献者的复现保证**，用户装你的包时根本不会读它。前者宽，后者紧，互不冲突。

### 7.4 把这些预防措施写成测试

F-1-02 没有真的发生，所以测试没法"复现故障"。它们检查的是**预防措施还在不在**：
哪天有人删了下界、删了锁文件、删了 `[build-system]`，测试会立刻变红。

在 `tests/test_packaging.py` 末尾追加：

```python
def test_F_1_02_every_dev_dependency_has_a_lower_bound(repo_root: Path) -> None:
    """An unbounded dependency lets CI and a laptop resolve different versions
    from the same commit, which makes 'works on my machine' unfalsifiable."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    with (repo_root / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    dev = cfg["project"]["optional-dependencies"]["dev"]
    assert dev
    for spec in dev:
        assert any(op in spec for op in (">=", "==", "~=")), f"unbounded: {spec}"


def test_F_1_02_lockfile_is_committed(repo_root: Path) -> None:
    assert (repo_root / "uv.lock").exists(), "uv.lock pins the exact resolution"


def test_F_1_02_build_backend_is_declared(repo_root: Path) -> None:
    """Without [build-system], tools fall back to setuptools -- and the
    fallback differs between them.  An implicit build is an unreproducible one.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    with (repo_root / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    assert cfg["build-system"]["build-backend"] == "hatchling.build"
```

> - **`try: import tomllib / except ModuleNotFoundError: import tomli as tomllib`**：
>   这就是 §7.1 那个环境标记要配合的代码。Python 3.11 及以上有标准库 `tomllib`；
>   3.10 上没有，就改用装好的 `tomli`，并用 `as tomllib` 起同一个名字，后面的代码不用区分。
> - **`with (...).open("rb") as fh:`**：以二进制方式打开文件（`tomllib` 要求这样），
>   `with` 保证用完自动关闭文件。
> - **`tomllib.load(fh)`** 把 TOML 读成一个**字典**。`cfg["project"]["optional-dependencies"]["dev"]`
>   就是一层层按键取值，最后拿到 §7.1 里那个依赖列表。
> - **`any(op in spec for op in (">=", "==", "~="))`**：只要这三种写法里有一种出现在
>   依赖字符串里，就算有下界。`any(...)` 里只要有一项为真，结果就为真。
> - **`assert 条件, "说明"`**：逗号后面的字符串是失败时打印的提示。`f"unbounded: {spec}"`
>   会直接告诉你是哪个依赖没写下界。

最后一个测试就是 §6.6 反向验证时那个"多红了的测试"：它和打开 wheel 的测试一起，
从两个角度盯着构建后端——一个检查配置写没写，一个检查打出来的东西对不对。

### 7.5 提交

§5.1 里 pytest 是临时用 `uv pip install pytest` 装的，这台电脑以外的人并不知道要装它。
现在它被正式写进了 `pyproject.toml`，锁定的版本也写进了 `uv.lock`。

```bash
git status          # 应该看到 pyproject.toml 和 tests/test_packaging.py 改了，uv.lock 是新文件
git add .
git commit
```

```
chore: declare dev dependencies and commit the lock file

pytest was installed by hand until now, so nobody else could run the tests
without guessing what to install.

Dependency ranges in pyproject.toml say what we accept; uv.lock says what
was actually resolved. CI will install with --frozen, so a lock file that
disagrees with pyproject.toml fails loudly instead of re-resolving. Tests
check that every dev dependency has a lower bound, that uv.lock exists and
that the build backend is declared.
```

### 7.6 顺手：把 §4.1 的 `.gitignore` 也钉住

§4.1 写 `.gitignore` 的时候还没有 pytest。现在有了，补一个测试，防止哪天有人清理
`.gitignore` 时把密钥那几行删掉：

```python
@pytest.mark.parametrize("pattern", [".env", "__pycache__/", "*.key", ".venv/"])
def test_F_1_03_gitignore_covers_the_usual_accidents(repo_root: Path, pattern: str) -> None:
    assert pattern in (repo_root / ".gitignore").read_text(encoding="utf-8")
```

> **`@pytest.mark.parametrize("pattern", [...])`**：把同一个测试用列表里的每个值各跑一次。
> 这里列表有 4 项，pytest 就会跑 4 个测试，每次 `pattern` 是其中一个值。
> 哪一项缺了，报告里会精确地写出是哪一项。这比在一个测试里写 4 个 `assert` 好：
> 那样第一个失败后，后面的就不跑了。

这个测试和依赖无关，单独提交：

```bash
git status          # 应该只看到 tests/test_packaging.py 改了
git add .
git commit -m "test: pin the .gitignore entries that keep secrets out"
```

> 这次只有一行标题，没什么"为什么"需要额外解释，所以直接用 `-m`。

---

## §8 验证 F-1-05：CI 会不会重到被绕过

追问二的后一半："在别人那里还成吗"。我只有一台电脑，而这台电脑上装了很多我已经
忘了自己装过的东西。答案是 CI。但 §3 里也担心过 CI 本身的风险：配得太重，大家就会
绕过它。

同样，这一条本章也没有真的撞上，它是**按规则预防**的。

### 8.1 先解决"这行代码该怎么排版"

在配 CI 之前先加一个格式化工具，理由不是审美：

**两个人的编辑器保存时的格式化行为不同，于是每次 diff（改动对比）里都混着一堆无关的行。**
审查的人要在 30 行变更里找出真正改了逻辑的那 2 行。

```toml
[tool.ruff]
line-length = 100
target-version = "py310"
src = ["src", "tests"]

[tool.ruff.lint]
select = [
    "E", "F", "W",   # pycodestyle + pyflakes: the classic errors
    "I",             # import sorting
    "UP",            # rewrite to modern syntax
    "B",             # bugbear: real bugs, not style
    "ASYNC",         # async footguns; nothing async yet
    "RUF",
]
```

> **ruff 是什么**：一个工具同时做四件事——格式化代码、排序 import、静态检查（不运行代码，
> 光读代码找问题）、把旧写法改成新写法。一个工具、一份配置。

`select` 里的字母是**规则组**。前面几组是常规，`B` 和 `ASYNC` 值得单独说：

- **`B`（bugbear）**：抓的是真 bug 而不是风格。比如 `def f(x=[])`——默认参数用了可变的
  列表，所有调用会共享同一个列表。
- **`ASYNC`**：异步代码的陷阱。**现在一行异步代码都没有，为什么要打开？**
  因为一旦开始和模型通信，代码就会变成异步的，而这类错误的特点是**不报错，只是悄悄
  变慢**。到时候你会看到它在 bug 刚写出来的那一刻就拦住它。

格式化和检查是分开的两条命令：

```bash
uv run ruff format .      # 排版
uv run ruff check .       # 找问题
```

第一次跑 `ruff format .` 可能会改动你已经写好的文件。这没关系，把配置和它改出来的
格式一起提交：

```bash
git status          # 应该看到 pyproject.toml，可能还有几个被重新排版的 .py 文件
git add .
git commit
```

```
chore: add ruff for formatting and linting

Two editors that format differently on save turn every diff into noise, and
the one line that changed behaviour hides among thirty that did not.

ASYNC is enabled before there is any async code: those mistakes do not
raise, they only make things slower, so the rule has to be on before the
first one is written.
```

### 8.2 CI

> **CI（持续集成）**：你把代码推送到 GitHub 时，一台干净的机器自动把你的项目从头装一遍、
> 跑一遍测试。它的价值就一句话：**在一台不是你的电脑上验证。**

在 GitHub 上，CI 的配置写在 `.github/workflows/ci.yml` 里：

```yaml
name: ci

on:
  pull_request: {}
  push:
    branches: [main]

jobs:
  check:
    runs-on: ubuntu-latest
    # A hung job blocks every pull request queued behind it.
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5
        with:
          enable-cache: true

      - name: Install dependencies
        # --frozen refuses to re-resolve: if uv.lock disagrees with
        # pyproject.toml the job fails instead of quietly installing something
        # different from what the author tested.
        run: uv sync --frozen --all-extras

      - name: Lint
        run: |
          uv run ruff format --check .
          uv run ruff check .

      - name: Test
        run: uv run pytest
```

逐段：

> - **`on:`** 什么时候跑。这里是"有人提合并请求（pull request）时"和"推送到 main 时"。
> - **`jobs:`** 一组任务。`check` 是我们给它起的名字。
> - **`runs-on:`** 在什么机器上跑。
> - **`steps:`** 按顺序执行。`uses:` 是调用别人写好的步骤，`run:` 是直接跑命令。
> - **`timeout-minutes: 10`** 现在就加——一个卡住的任务会堵住排在它后面的所有合并请求。

**除了拉代码和装 uv，真正干活的只有三步：装依赖、检查、测试，跑完不到一分钟。**
明确没有加的：多平台、多 Python 版本、覆盖率门槛、安全扫描、依赖审计。

这不是偷懒，而是一条明确的规则：

> **一个检查要进"能挡住合并"的 CI，前提是它拦下过真实的问题。**

反过来会发生什么？项目还没什么功能，CI 已经要跑 18 分钟；某个检查因为外部服务不稳定
而时不时失败；有人加了 `continue-on-error: true` 让它失败也不算数；最后所有人都学会了
看见红叉直接合并。

**一个被绕过的检查，保护效果等于零**，而且比没有检查更糟——它给了你虚假的安全感。

一个新检查的正当理由应该长这样："上周有个 bug 溜进了 main，这个检查能拦住它。"
而不是"最佳实践建议配置这个"。

这条规则光写在教程里是守不住的，得写成测试。在 `tests/test_packaging.py` 顶部的 import
里补上 `import yaml`（它来自 §7.1 里的 `pyyaml`），然后在末尾追加：

```python
def test_F_1_05_ci_is_valid_yaml_and_stays_small(repo_root: Path) -> None:
    wf = yaml.safe_load((repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))

    # PyYAML parses the key `on:` as the boolean True (a YAML 1.1 quirk).
    triggers = wf.get("on", wf.get(True))
    assert "pull_request" in triggers

    steps = wf["jobs"]["check"]["steps"]
    names = [s.get("name", "") for s in steps]
    assert any("Lint" in n for n in names)
    assert any("Test" in n for n in names)
    assert len(steps) <= 6, "the blocking suite is meant to stay fast"


def test_F_1_05_ci_job_is_bounded(repo_root: Path) -> None:
    """A hung job blocks every pull request queued behind it."""
    wf = yaml.safe_load((repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert wf["jobs"]["check"]["timeout-minutes"] <= 15
```

> - **`yaml.safe_load(...)`** 把 YAML 文本读成字典和列表。如果 CI 文件有语法错误，
>   这一步就会报错——这本身就是一项检查，否则要等推送到 GitHub 才知道写错了。
> - **`wf.get("on", wf.get(True))`**：YAML 有个老规矩，会把 `on` 这个词当成布尔值 `True`，
>   所以 `on:` 这个键读出来可能叫 `True` 而不是 `"on"`。`dict.get(键, 默认值)` 在键不存在时
>   返回默认值，这里就是"先按 `"on"` 找，找不到再按 `True` 找"。
> - **`[s.get("name", "") for s in steps]`**：列表推导式，把每个步骤的名字取出来组成列表；
>   没有名字的步骤（比如 `actions/checkout`）就用空字符串。
> - **`len(steps) <= 6`**：这是整个测试的重点。现在有 5 个步骤，留了一个余量。
>   想加第七步的人会先撞上这个测试，被迫回答"它拦下过什么"。
> - **`timeout-minutes <= 15`**：超时不能被删掉，也不能被随手调大。

> **这套 CI 之后会长成什么样**：等检查多到一定程度，它会自然分成两层，和 codex 现在一样——
> 一层快的，挡住合并；一层慢的，合并之后再跑，覆盖所有平台。
> codex 的那个"挡合并"的文件现在有 7 个检查，**是三年里一个个长出来的。**

### 8.3 提交

```bash
git status          # 应该看到 .github/workflows/ci.yml 和 tests/ 下的改动
git add .
git commit
```

```
ci: run install, lint and tests on every pull request

Three steps and a ten-minute timeout. A check joins the blocking suite
only after it has caught a real problem; a slow or flaky suite teaches
people to merge through red, and a check that gets bypassed protects
nothing.
```

> CI 要真的跑起来，需要把仓库推送到 GitHub。在 GitHub 上建仓库和推送不在本章范围内，
> 本地的测试会先检查 CI 配置本身有没有写对。

---

## §9 回头看：这一章的提交

### 9.1 七个 commit 是怎么来的

现在敲 `git log --oneline`，会看到这样的历史（最新的在最上面；每行开头是 commit 的
编号，叫 hash，你的会不一样，这里省略了）：

```
ci: run install, lint and tests on every pull request
chore: add ruff for formatting and linting
test: pin the .gitignore entries that keep secrets out
chore: declare dev dependencies and commit the lock file
fix: ship the prompt file inside the wheel
fix: make tests run against the installed package, not the source tree
chore: make minicodex installable as a package
```

这七个 commit **不是做完之后回头切出来的**，而是每做完一个能跑的小步就当场提交的结果。
message 也是当场写的——"为什么这么做"在当下记得最清楚，半年后再补，只能写出
"做了什么"。

什么时候算"一个能跑的小步"？判断标准**不是大小，而是能不能单独撤销**：撤掉 CI，
不该把打包配置一起带走。§5.5 把挪目录和新测试放在一个 commit 里，是因为它们是同一个
想法，撤掉其中一个，另一个就没意义了。

一个实用的自检：**如果标题里非要用 "and"，那就是两个 commit。**

### 9.2 message 为什么这么写

**用 `类型: 一句话` 的格式**（这套约定叫 Conventional Commits），好处不是"看起来专业"，
而是**扫一眼 `git log` 就知道哪些改变了行为**。`chore`、`ci` 通常不影响程序本身，
`fix` 要重点看。

**标题写做了什么，正文写为什么。** 因为"做了什么"用 `git show` 一秒就能看到，半年后
没人会问这个。他们会问的是：

> 为什么源码非要放在 `src/` 底下？我能挪出来吗？
> 这个 0.3 秒的测试看着没用，能删吗？

§5.5 和 §6.7 那两条 message 的正文，回答的就是这两个问题。

**几种会让未来的你痛苦的写法**：

| 写法 | 问题 |
|---|---|
| `initial commit` | 每个仓库都有一个，没有一个有用 |
| `update` / `wip` | 你在给未来的自己出谜语 |
| `fix bug` | 修了哪个 bug？为什么这么修？全都没说 |
| `add src layout, ruff, tests and CI` | 一个 commit 做了四件事，哪一件都没法单独撤销 |

### 9.3 推送之前可以整理

边写边提交，难免会有零碎的 commit：刚提交完发现漏了一个文件、写错了一个字。
在**推送到 GitHub 之前**，这些都可以收拾：

```bash
git add 漏掉的文件
git commit --amend       # 把这次的改动补进上一个 commit，顺便可以改 message
```

```bash
git add -p               # 一个文件里改了两件不相干的事时，逐块询问要不要放进这次提交
```

> **只整理还没推送出去的 commit。** 已经推送、别人可能已经拉下来的历史不要改，
> 否则别人的仓库会和你的对不上。
>
> 更进一步的整理（合并、重排多个 commit）用 `git rebase -i`，新手暂时用不到。
> 另外，很多团队在合并 PR 时会把整个 PR 压成一个 commit（squash merge），
> 这时 PR 的标题和描述就比其中每个 commit 更重要。

### 9.4 分支

本章直接提交在 `main` 上。**这是全书唯一一次。**

理由：脚手架没有"行为"可以审查（review）。从下一章开始，每个功能都在一个单独的
分支上做，做完再合并回 `main`：

```bash
git checkout -b feat/agent-loop     # 新建一个叫 feat/agent-loop 的分支并切过去
```

命名照抄 codex（本书参照的 OpenAI 编码 Agent）的 `docs/contributing.md`：`feat/xxx`、`fix/xxx`。

> **为什么不用 git flow**（那套 develop/release/hotfix 分支）：它是为"同时维护多个
> 已发布版本"设计的，而我们只有一条线。**流程的复杂度要匹配问题的复杂度**，
> 多出来的那部分只会变成负担。

---

## §10 回顾：猜对了几条

回到 §3 那张表，逐条对账：

| 编号 | 动工前的猜测 | 结果 | 如果不防，它会怎么暴露 | 挡住它的东西 |
|---|---|---|---|---|
| F-1-01 | 测试测的是源码目录 | ✅ **撞上了**（§5） | 🟡 卸载了包测试还是绿 | src layout + 登记信息断言 |
| F-1-01 | *（没猜到）* 可编辑安装掩盖了 wheel 缺文件 | ⚠️ **意外**（§6） | 🟡 测试全绿，用户装上崩 | 显式构建后端 + 打开 wheel 检查的测试 |
| F-1-02 | 本地和 CI 装出不同依赖 | 🛡 预防，未撞上（§7） | 🔴 CI 红、本地绿 | 锁文件 + `--frozen` + 显式构建后端 |
| F-1-03 | 密钥进仓库 | 🛡 预防，未撞上（§4.1） | ⚫ 收到密钥泄露告警邮件 | 写代码之前先写 `.gitignore` |
| F-1-05 | CI 太重被绕过 | 🛡 预防，未撞上（§8） | 🟣 发现大家都在红着叉合并 | 三步起步，按证据增长 |

"会怎么暴露"那一列的标记，全书通用：

🔴 崩溃 · 🟡 静默错误 · 🟢 主动边界测试 · 🔵 长时间运行才出现 · 🟠 看日志发现 ·
🟣 代码审查发现 · ⚫ 用户报告 · ⚪ lint/类型检查

从这张表里能看出三件事：

**第一，最危险的那个是没猜到的。** F-1-01 猜到了一半：猜到了"import 来自源码目录"，
没猜到"即使装上了，可编辑安装也读源码目录"。§6.6 那个"把修复改回去看测试红不红"的
习惯，就是为猜漏的部分准备的——你没法保证猜全，但可以保证每个测试真的有用。

**第二，两次撞上的都是 🟡。** 它们不报错，测试还是绿的，一切看起来都好——直到用户装上。
全书的故障档案里，这种不会自己报错的占了绝大多数（统计见 `FAULTS.md`）。
**你不主动去撞，它们就不会出现。**

**第三，"预防"和"撞上"要分开写。** 后三条本章都没有真的发生。如实写"预防，未撞上"，
比假装每条都亲眼见过更有用：读者知道哪些结论有实证，哪些只是按常识防的。

所以本章真正的产出不只是一个能装的包，还有**三个会替你去撞问题的东西**：
一个不白送 import 的目录结构、一个会打开 wheel 检查的测试、一台不是你的电脑。

---

## §11 三条主线各自留下了什么

### 主线 A · 需求变代码

**方法**：把目标里的每个词拆开问"这需要什么"，直到答案是具体动作。然后追问两个问题——
**我怎么验证？** 和 **明天、在别人那里还成吗？**

大部分工程化的东西都是这两个追问的答案。它们不是外部规范强加的，**是需求本来就有、
只是没说出口的部分。**

**顺序**：从"不做就没法验证下一步"的那个开始。

### 主线 B · 工程化交付

| 动作 | 本章的规则 |
|---|---|
| 什么时候 commit | 一个能单独撤销的完整想法完成时 |
| message 怎么写 | 标题写做了什么，正文写**为什么**和**放弃了哪些方案** |
| 什么时候开分支 | 有行为变更就开；纯脚手架可以不开 |
| 什么时候加 CI 检查 | 它拦下过真实的问题才加 |
| `.gitignore` 什么时候写 | `git init` 之后、写代码之前，因为密钥泄露只能预防 |
| 锁文件要不要提交 | 要。它和依赖版本范围是两回事 |

### 主线 C · 故障

**动工前先猜，猜完逐条去撞，撞完如实记录**——成立、不成立、预防未撞上、没猜到，
四种结果都要写下来。

**本章最重要的一招**，会在全书反复出现：

> **把一个悄悄出错的场景，改造成一个大声报错的场景。**

src layout 没有"修好"任何 bug。它做的是让"没安装"这件事从**静默通过**变成
**测试收集阶段就报错**。修的是一类问题的可见性，不是一个问题的实例。

**第二招**：

> 写完修复，花三十秒把修复改回去，确认测试会红。
> 一个对着坏代码也能通过的测试，是装饰品。

**第三招**（来自本章那个没猜到的意外）：

> 测你**交付**的东西，不是你**开发**的东西。
> 可编辑安装读源码目录，所以它对打包问题完全免疫——包括对打包 bug 免疫。

---

## 如果你只记住三件事

1. **`import` 找得到 ≠ 已安装。** 当前目录会白送你一次 import，而这次白送会让你的测试
   对一整类打包问题视而不见，直到用户装上才崩。

2. **动工前先猜故障，做完再逐条验证。** 追问两遍：我怎么验证它成了？它明天、在别人那里
   还成吗？测试、CI、锁文件都是这两个问题的答案，不是规范强加的负担。

3. **把静默失败改造成响亮失败。** 这比修好一个 bug 更值——你修的是一类问题的可见性。

---

## 动手

```bash
cd steps/step-1_setup
uv sync --all-extras
uv run minicodex --version
uv run pytest
```

这就是 §1 说的"三条命令"（`cd` 只是进入目录）：装依赖、跑命令、跑测试。

预期输出：

```
minicodex 0.0.1
python    3.10.12 (linux)

..............                                                           [100%]
14 passed in 0.37s
```

你的 Python 版本和平台会不一样，Windows 上显示的是 `(win32)`。

**建议你自己做一遍的两件事**（十分钟，比读十页有用）：

1. 把 `pyproject.toml` 里的 `[build-system]` 那段删掉，跑 `uv run pytest`。看哪个测试红了，
   读它的报错。
2. 把 `src/minicodex/` 挪到项目根目录，改 `pyproject.toml` 让它能装上，然后
   `uv pip uninstall minicodex` 再跑测试。看看还剩几个测试能发现问题。

---

## 选读 A · 编辑器配置

> 这一节不影响后面的内容，第一次读可以跳过，调试 Agent 时再回来看。

`steps/step-1_setup/.vscode/` 里的三个文件跟着仓库走。不是强迫别人用 VS Code，而是让用
它的人**在编辑器里就看到和 CI 一样的提示**，不用等 CI 红了才知道。

**`extensions.json`**：第一次打开项目时，VS Code 会弹窗推荐装这几个插件。

```json
{
  "recommendations": [
    "ms-python.python",
    "ms-python.vscode-pylance",
    "charliermarsh.ruff",
    "tamasfe.even-better-toml"
  ]
}
```

> 依次是：Python 支持、代码补全和类型检查（Pylance）、ruff、TOML 文件高亮。

**`settings.json`**：让编辑器的行为和 CI 一致。

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "editor.formatOnSave": true,
  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff",
    "editor.codeActionsOnSave": { "source.organizeImports": "explicit" }
  },
  "python.analysis.typeCheckingMode": "basic",
  "python.testing.pytestEnabled": true,
  "python.testing.pytestArgs": ["tests"]
}
```

> - `defaultInterpreterPath`：默认用项目虚拟环境里的 Python。`${workspaceFolder}` 是项目根目录。
>   **Windows 用户注意**：这个路径是 macOS/Linux 的写法，Windows 上要改成
>   `${workspaceFolder}/.venv/Scripts/python.exe`，或者在 VS Code 左下角手动选一次解释器。
> - `formatOnSave` + `defaultFormatter`：保存时用 ruff 排版，和 CI 里的 `ruff format --check` 一致。
> - `organizeImports`：保存时自动整理 import 的顺序，对应 ruff 的 `I` 规则组。
> - `typeCheckingMode: "basic"`：让 Pylance 根据类型标注检查明显的错误，比如把字符串传给了
>   要整数的参数。
> - 最后两行：在 VS Code 的测试面板里用 pytest 跑 `tests/` 下的测试。

**`launch.json`**：调试配置。在 VS Code 的"运行和调试"面板里选中一项，按 F5 就能在调试器里
跑起来，可以打断点、单步执行。

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "minicodex --version",
      "type": "debugpy",
      "request": "launch",
      "module": "minicodex",
      "args": ["--version"],
      "console": "integratedTerminal",
      "justMyCode": false
    },
    {
      "name": "pytest: current file",
      "type": "debugpy",
      "request": "launch",
      "module": "pytest",
      "args": ["${file}", "-vv"],
      "console": "integratedTerminal",
      "justMyCode": false
    }
  ]
}
```

> - 第一项相当于在调试器里执行 `python -m minicodex --version`。
> - 第二项调试当前打开的测试文件（`${file}`），`-vv` 让 pytest 打印每个测试的名字和详细信息。

如果你也加了这三个文件，单独提交一次，例如 `chore: share VS Code settings`。
§9 列出的七个 commit 里没有它，因为它是选读内容。

调试 Agent 时真正省时间的是这几个：

- **`"justMyCode": false`** —— 调试器默认不让你单步进入第三方库的代码。可遇到库的
  奇怪行为时，你最需要的恰恰是进去看看。现在就关掉。
- **条件断点** —— Agent 的 bug 很少出在第 1 轮，常常在第 23 轮。在断点上右键，填
  `turn_index == 23`，只在第 23 轮停下。否则你只能加 print 重跑，一次两分钟。
- **`pytest -x --lf --pdb`** —— `-x` 遇到第一个失败就停；`--lf` 只跑上次失败的；
  `--pdb` 失败时立刻进入调试器。修 bug 时的默认用法。

> **PyCharm 用户**：三条都有对应功能。`justMyCode` 对应
> Settings → Debugger → Python → Do not step into library scripts。

---

## 选读 B · codex 是怎么做的

> 这一节是和 codex（OpenAI 用 Rust 写的编码 Agent，本书参照的对象）的对照。
> 不读不影响后面的内容。

**`.vscode/` 是同一套思路。** codex 的 `extensions.json` 推荐 rust-analyzer、ruff、
even-better-toml；`launch.json` 里有 "Cargo launch" 和 **"Attach to running codex CLI"**。
注意第二个——**挂到一个已经在运行的程序上调试**，这是调试长时间运行的 Agent 的常用手段，
Python 里对应的是 `debugpy.listen()`。

**CI 是分层的。** `blocking-ci.yml` 顶上一行注释：

> This is the single entrypoint for checks that block a PR merge.

底下挂着 7 个可复用的 workflow。另一个入口 `postmerge-ci.yml` 的注释写着
"intentionally outside the merge-blocking suite"，重的检查都放在那里。

里面有一段特别值得看：

```yaml
  required:
    name: CI required
    # Without `always()`, GitHub skips this job after a failed dependency and a
    # required check can appear successful instead of reporting the failure.
    if: ${{ always() }}
```

这是一个吃过亏才知道的 GitHub Actions 陷阱：前面的任务失败时，这个汇总任务会被跳过，
而"跳过"在 GitHub 的合并保护规则里可能被当成"通过"。**这条注释解释的不是代码在做什么，
而是为什么必须这么做**——和我们对 commit message 正文的要求完全一致。

**`AGENTS.md`。** codex 根目录有一份写给 AI 看的项目规约，其中几条很有意思：

> resist adding code to codex-core!

> Target Rust modules under 500 LoC, excluding tests. If a file exceeds roughly
> 800 LoC, add new functionality in a new module...

> Do not create small helper methods that are referenced only once.

（LoC 是 lines of code，代码行数；codex-core 是 codex 的核心模块。）

**这不是设计文档，是护栏。护栏是撞过之后才装的。** 第一条甚至能看出它的来历：核心模块
已经成了整个项目里最大的一块，因为"往 core 里加代码"永远比"拆出一个新模块"省事。

我们的规约会用同样的方式生长：**撞一次，加一条。**

---

**下一章**：[最小 Agent 循环](ch00-minimal-loop.md)——写一个 while 循环，然后发现
"模型说它读了文件"和"文件真的被读了"是两件事。

---

# 附录 · 对照检查

## T1 · 每个文件是在哪一节写出来的

本章结束时，你的项目内容应该和 `steps/step-1_setup/` 一致（测试函数的先后顺序、注释可以不同）。对不上时，按这张表找到
对应的小节回去看：

| 文件 | 在哪写的 |
|---|---|
| `.gitignore` | §4.1 |
| `README.md` | §4.8 写开头，§6.1 补最后一段 |
| `pyproject.toml` | `[project]` 前两项和 `[project.scripts]`：§4.5、§4.6；其余三项：§4.8；`[tool.pytest.ini_options]`：§5.4；`[build-system]` 和 `[tool.hatch...]`：§6.4；依赖：§7.1；`[tool.ruff...]`：§8.1 |
| `uv.lock` | §7.3，由 `uv lock` 生成，不用手写 |
| `src/minicodex/__main__.py` | §4.7 |
| `src/minicodex/__init__.py` | §6.1 |
| `src/minicodex/prompts/system.md` | §6.1 |
| `tests/conftest.py` | §6.5 |
| `tests/test_packaging.py` | §5.4 新建，§6.1、§6.5、§7.4、§7.6、§8.2 各追加一部分 |
| `.github/workflows/ci.yml` | §8.2 |
| `.vscode/*.json` | 选读 A |

`steps/` 里的 `pyproject.toml` 和各个文件还带着一些英文注释，解释每段配置为什么存在。
内容和正文讲的一致，可以对照着读。

一个正文没展开的小点：`__init__.py` 里的 `__version__ = "0.0.1"` 是**版本号唯一的来源**。
`__main__.py` 从这里 import 它；以后做发布脚本时，也从这里读取。版本号只存一处，
就不会出现"两个地方写的版本不一样"。（`pyproject.toml` 里的 `version` 目前还是另写的一份，
这个重复要到做发布的时候才处理。）

## T2 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| `uv: command not found` | 没装 uv，或装完没重开终端 | 按 §0.1 安装，然后关掉终端重新打开 |
| `pytest: command not found` | pytest 没装进当前虚拟环境 | `uv sync --all-extras`，然后用 `uv run pytest` |
| Windows 上找不到 `.venv/bin/python` | Windows 的路径不一样 | 用 `.venv\Scripts\python.exe`，或直接用 `uv run python` |
| `system_prompt()` 找不到 system.md | 用了 `Path("prompts")`，相对的是当前目录 | 改用 `Path(__file__).parent / "prompts"`，相对包目录 |
| wheel 装上后 `system_prompt` 报错 | 数据文件没进 wheel | 测试打开构建出的 wheel，断言文件在里面（§6.5） |
| `from minicodex import *` 带进了不想导出的东西 | 没写 `__all__` | 显式写 `__all__ = ["__version__", "system_prompt"]` |
