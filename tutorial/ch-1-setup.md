# 第 -1 章 · 把一个想法变成一个能装的东西

> **代码**：`steps/step-1_setup/`
> **分支**：`main`
> **产出**：一个别人能装上、能在终端里跑起来的 Python 包
> **前置**：你会写 Python。工程化的东西一概不需要提前知道。

---

## §1 这一章要做出来的东西

一条命令：

```bash
minicodex --version
```

看起来毫无难度。但加两个约束之后，它就变成了一整章：

1. 这条命令要能在**一台没见过这个仓库的电脑**上跑起来
2. 从零到跑起来，**不超过三条命令**

为什么是这个目标而不是"先把 Agent 写出来"？

因为后面十七章的每一步，都要回答同一个问题：**我改的这个东西，到底有没有真的生效？**
而这个问题的前提是——你的代码得先能被**装**到一个确定的地方去，测试跑的得是**那个**
东西，而不是你硬盘上碰巧躺着的某个文件夹。

这两件事不是自动成立的。本章的一半篇幅是在证明它们不自动成立。

---

## §2 先把目标翻译成待办

这一步是本书的第一条主线：**怎么把一句需求，变成一份你知道该先做哪个的清单。**

方法很笨，但是有效：**把目标里的每个词拆开问"这需要什么"**，一直问到答案是一个具体
动作为止。

> "让**别人**在**新电脑**上，用**三条命令**，跑起来 `minicodex --version`"

拆开：

| 目标里的词 | 追问 | 需要做的事 |
|---|---|---|
| 别人 | 别人怎么拿到我的代码？ | 代码要能被**打包**成一个标准格式 |
| 新电脑 | 他电脑上没有我的依赖 | 依赖要**声明**出来，让工具自动装 |
| 跑起来 | 装完之后凭什么能跑？ | 要**装到 Python 找得到的地方** |
| `minicodex` 这条命令 | 为什么敲这个词就有反应？ | 要注册一个**命令入口** |
| 三条命令 | 哪三条？ | 建环境 / 装 / 跑 |
| （隐含）我怎么知道成了 | 光看它打印出来不算 | 要有**测试** |
| （隐含）明天还成吗 | 依赖升级了呢？ | 要**锁版本** |
| （隐含）别人机器上也成吗 | 我只有一台电脑 | 要有 **CI** |

八行。前五行是需求直接推出来的，后三行是**追问"我怎么知道"和"以后还成吗"逼出来的**。

> **这个追问是本书最重要的一个思维习惯。** 任何一个需求，做完之后都要再问两遍：
> **我怎么验证它真的成了？** 和 **它明天/在别人那里还成吗？**
> 大部分工程化的东西——测试、CI、锁文件、类型标注——都是这两个问题的答案。
> 它们不是"规范要求的"，是需求的一部分，只是需求没明说。

顺序也不用纠结：**从"不做就没法验证下一步"的那个开始。** 这里就是"能装"。

---

## §3 一步一步做出来

从空目录开始。每一步都真的跑一遍，报错原样贴出来。

### 3.1 先写个能跑的东西

```
minicodex/
├── minicodex/
│   ├── __init__.py
│   └── __main__.py
```

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

跑：

```
$ python3 -m minicodex
minicodex 0.0.1
```

成了。**但只在这个目录里成。** 换个地方：

```
$ cd /tmp && python3 -m minicodex
/usr/bin/python3: No module named minicodex
```

这个报错值得停下来看清楚，因为**理解它，等于理解了后面所有事情的一半**。

### 3.2 插播：`import` 到底在干什么

Python 执行 `import minicodex` 时，会按顺序翻一个列表——`sys.path`——找有没有叫这个
名字的东西。

```
$ python3 -c "import sys; print(repr(sys.path[0]))"
''
```

第一项是 `''`，代表**当前工作目录**。

所以 `python3 -m minicodex` 在项目目录里能跑，纯粹是因为**当前目录里正好有个叫
`minicodex` 的文件夹**。这和"这个包被正确安装了"没有任何关系。

那"安装"是什么？**安装就是把你的代码复制（或者链接）到一个已经在 `sys.path` 上的
目录里去**，通常叫 `site-packages`。

一句话记住：

> **`import` 找得到 ≠ 已安装。当前目录白送你一次 import，而它会骗你一整章。**

这句话在 §4 会兑现成一个非常难看的场面。

### 3.3 最小的 `pyproject.toml`

现在要让它能被安装。先试试什么都不加：

```
$ uv pip install -e .
error: /tmp/lab/step1 does not appear to be a Python project,
as neither `pyproject.toml` nor `setup.py` are present in the directory
```

工具说：**我不知道这是个什么东西。** 它需要一份说明书，文件名固定叫 `pyproject.toml`。

> **`pyproject.toml` 是什么**：Python 官方定的项目说明书，TOML 格式（比 JSON 多了
> 注释，比 YAML 少了缩进陷阱）。所有工具——pip、uv、poetry、hatch——读的都是同一份。
> `[xxx]` 开一个段落，段落里是 `键 = 值`。

先写能让它跑起来的**最少内容**：

```toml
[project]
name = "minicodex"
version = "0.0.1"
```

> `[project]` 这个段名不是随便起的，是标准（PEP 621）规定的。工具认这个名字。
> `name` 是别人 `pip install` 时敲的名字，`version` 是版本号。就这两个是必填。

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

装上了。验证一下：

```
$ cd /tmp && /tmp/lab/step1/.venv/bin/python -m minicodex
minicodex 0.0.1
```

**换目录也能跑了。** 三行 TOML 换来的。

> **`-e` 是什么**：可编辑安装（editable install）。不复制代码，而是在 `site-packages`
> 里留个指针指回你的源码目录。**改代码立刻生效，不用重装。** 开发时永远用 `-e`。
> 它也会在 §4 里害你一次——记住这个伏笔。

### 3.4 变成一条命令

目标是敲 `minicodex`，不是敲 `python -m minicodex`。现在有这个命令吗？

```
$ ls .venv/bin/
activate  activate.bat  activate.csh  ...  python  python3  python3.10

$ minicodex
bash: minicodex: No such file or directory
```

没有。因为**我没告诉任何人"我想要一条叫这个名字的命令"**。加上：

```toml
[project.scripts]
minicodex = "minicodex.__main__:main"
```

> **怎么读这一行**：等号左边是要生成的命令名。右边是 `模块路径:函数名`——
> "去 `minicodex.__main__` 这个模块里，找一个叫 `main` 的函数，调用它"。
> 安装时，工具会在 `.venv/bin/` 下生成一个小脚本干这件事。
> 这个机制叫 **entry point（入口点）**。

重装，再看：

```
$ ls .venv/bin/ | grep minicodex
minicodex

$ cd /tmp && /tmp/lab/step1/.venv/bin/minicodex
minicodex 0.0.1
```

**§1 的目标达成了。** 五行 TOML。

### 3.5 顺手补上 `--version` 该有的样子

```python
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
```

为什么要打印 Python 版本和平台？

因为一年后你会收到这样的 issue：**"跑不了"**。没有版本号，没有系统信息，没有复现步骤。

`--version` 多打两行，等于给了对方一条"把这个贴上来"的指令。这是**你现在花两分钟、
以后省两小时**的典型例子——本书会遇到很多次。

`main(argv=None)` 这个参数也不是摆设：测试可以直接 `main(["--version"])` 调用，不用
去动 `sys.argv`。**一个函数能不能被测试，往往取决于它有没有一个可注入的入口。**

---

## §4 我怎么知道它真的成了

到这里，`minicodex --version` 能跑了。第一条主线的产出有了。

现在进入第二个追问：**我怎么验证？** ——这一节会撞上本章最难看的一幕。

### 4.1 第一个测试

```python
# tests/test_smoke.py
from minicodex import __version__


def test_version_is_set() -> None:
    assert __version__
```

```
$ pytest -q
.                                                                        [100%]
1 passed in 0.00s
```

绿了。

### 4.2 现在把包卸载掉

```
$ uv pip uninstall minicodex
Uninstalled 1 package in 1ms
 - minicodex==0.0.1

$ python -c "import importlib.metadata as m; print(m.version('minicodex'))"
importlib.metadata.PackageNotFoundError: No package metadata was found for minicodex
```

包确实没了。再跑测试：

```
$ pytest -q
.                                                                        [100%]
1 passed in 0.00s
```

**包被卸载了，测试依然全绿。**

看看它 import 到的是什么：

```
$ python -c "import minicodex; print(minicodex.__file__)"
/tmp/lab/step1/minicodex/__init__.py
```

源码目录。就是 §3.2 说的那件事：**当前目录白送的那次 import。**

结论很难受：**这个测试套件从来没有测过"安装"这件事。** 它测的是"我硬盘上有这个
文件夹"。

### 4.3 这真的会造成损失吗

会。而且是最糟的那种：**测试全绿，用户装上就崩。**

演示一下。给包加一个数据文件——一个提示词模板，Agent 项目里迟早会有一堆：

```
minicodex/
├── __init__.py
└── prompts/
    └── system.md
```

```python
def system_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")
```

```python
def test_prompt_loads() -> None:
    assert "coding agent" in system_prompt()
```

```
$ pytest -q
.                                                                        [100%]
1 passed in 0.00s
```

绿。现在把它真的打包成用户会拿到的那个文件，看看里面有什么：

```
$ uv build --wheel
$ python -c "import zipfile; [print(' ', n) for n in zipfile.ZipFile('dist/minicodex-0.0.1-py3-none-any.whl').namelist()]"
   minicodex/__init__.py
   minicodex-0.0.1.dist-info/METADATA
   minicodex-0.0.1.dist-info/WHEEL
   minicodex-0.0.1.dist-info/top_level.txt
   minicodex-0.0.1.dist-info/RECORD
```

**`system.md` 不在里面。**

> **wheel 是什么**：`.whl` 文件，Python 包的分发格式，本质是个 zip。
> `pip install` 拿到的就是它。**它就是用户实际收到的东西。**

模拟一个用户：

```
$ uv venv /tmp/user && uv pip install --python /tmp/user/bin/python dist/minicodex-0.0.1-py3-none-any.whl
$ /tmp/user/bin/python -c "from minicodex import system_prompt; print(system_prompt())"

FileNotFoundError: [Errno 2] No such file or directory:
'/tmp/user/lib/python3.10/site-packages/minicodex/prompts/system.md'
```

完整的因果链：

1. 我用的构建后端（默认回落的 setuptools）**不会把非 `.py` 文件放进 wheel**
2. 我的测试跑在可编辑安装上，**读的是源码目录**，文件当然在
3. 于是测试永远绿
4. 用户装上，崩

**注意第 2 步。** 那个我在 §3.3 说"开发时永远用 `-e`"的东西，正是让这个 bug 隐身的
原因。工具没有骗你，是你问的问题不对。

### 4.4 修

**第一步：换成 src layout。**

```
src/
└── minicodex/
    ├── __init__.py
    ├── __main__.py
    └── prompts/system.md
tests/
pyproject.toml
```

就是把包挪进 `src/`。为什么这一挪能解决问题？

因为现在仓库根目录里**没有**叫 `minicodex` 的文件夹了。当前目录那次白送的 import 送不
出去了。不安装就 import 不到：

```
$ pytest -q                                # 故意不装
tests/test_smoke.py:1: in <module>
    from minicodex import __version__
E   ModuleNotFoundError: No module named 'minicodex'
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.08s
```

**从"静默地绿"变成了"响亮地红"。** 这就是这次改动买到的全部东西，而它足够值。

> **一条可以带走的原则**：把一个悄悄出错的场景，改造成一个大声报错的场景。
> 这比"修好这个 bug"更有价值——因为你修的是**一类** bug 的可见性，
> 而不是一个 bug 的实例。本书会反复用这一招。

**第二步：显式声明构建后端。**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/minicodex"]
```

> **`[build-system]` 是什么**：告诉工具"用谁把我这个目录变成 wheel"。
> 这个角色叫**构建后端（build backend）**。`requires` 是它自己需要什么，
> `build-backend` 是从哪个模块调用它。
>
> **不写会怎样？** 不会报错——所有工具都会**悄悄回落到 setuptools**。
> 而不同工具的回落行为不完全一致。也就是说：**你打出来的东西取决于谁打的。**
> 这正是上面那个 bug 的第一层原因。
>
> **为什么选 hatchling 而不是 setuptools**：默认行为符合直觉——包目录下的所有文件
> 都进 wheel，包括 `.md`。setuptools 出于历史原因默认只收 `.py`，要额外配置。
> 一个"默认就对"的工具，价值远大于"配置完也能对"的工具。

`packages = ["src/minicodex"]` 其实可以不写，hatchling 能自动发现。写出来的理由是：
**一个靠自动推断的构建，会在你改名一个目录的时候悄悄换个行为。** 说出来，一行的成本。

现在再看 wheel：

```
   minicodex/__init__.py
   minicodex/__main__.py
   minicodex/prompts/system.md          ← 在里面了
   minicodex-0.0.1.dist-info/METADATA
   minicodex-0.0.1.dist-info/WHEEL
   minicodex-0.0.1.dist-info/entry_points.txt
   minicodex-0.0.1.dist-info/RECORD
```

### 4.5 把这次教训变成测试

修好不够。**下次有人改配置，怎么保证不会再犯？**

这是第三条主线的核心动作，本书每一章都会做一次：**把刚才吃的亏，写成一个以后会替你
喊疼的测试。**

```python
def test_F_1_01_package_is_installed_not_merely_importable() -> None:
    """Under a flat layout `import minicodex` succeeds from the repo root even
    when nothing was installed, because '' is on sys.path.  Asking for the
    metadata is a question only a real installation can answer.
    """
    try:
        assert version("minicodex")
    except PackageNotFoundError:
        pytest.fail("minicodex is not installed; run `uv sync --all-extras`")
```

> 用 `importlib.metadata.version()` 而不是 `import minicodex`——因为**元数据是源码目录
> 给不出来的东西**。这是"已安装"和"文件在那儿"唯一可靠的区分方式。

但**光有这个还不够**，而这一点我是踩了坑才知道的：

```python
def test_F_1_01_data_files_are_readable_at_runtime() -> None:
    assert "coding agent" in minicodex.system_prompt()
```

这个测试**在坏掉的配置下也是绿的**——可编辑安装读的是源码目录，文件永远在。

真正能抓住它的只有一个办法：**把 wheel 打出来，打开看**。

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

0.31 秒。整个文件里**唯一**能抓住这个 bug 的测试。

### 4.6 验证测试真的有用

一个绿色的测试不能说明它有用——它可能对着坏代码也一样绿（刚才那个
`system_prompt()` 测试就是）。

所以：**把修复改回去，看它红不红。**

把 `[build-system]` 换回 setuptools、去掉 `packages` 配置：

```
FAILED tests/test_packaging.py::test_F_1_01_built_wheel_actually_contains_the_data_file
FAILED tests/test_packaging.py::test_F_1_02_build_backend_is_declared
```

红了。改回来，全绿：

```
$ uv run pytest
..............                                                           [100%]
14 passed in 0.37s
```

> **一条可以带走的规则**：写完一个修复，花三十秒把修复改回去，确认对应测试会红。
> **一个对着坏代码也能通过的测试，不是回归测试，是装饰品。**
> 这是我知道的性价比最高的一个习惯，全书每一章都会做。

---

## §5 明天还能跑吗

第三个追问。

### 5.1 虚拟环境

到目前为止我一直在用 `.venv/bin/python`。解释一下为什么。

> **虚拟环境**是一个独立的、只属于这个项目的 Python 安装。装进去的包不会影响系统，
> 也不会被别的项目影响。
>
> **不用会怎样**：项目 A 要 `httpx 0.27`，项目 B 要 `0.28`，全局只能有一个，
> 于是你在两个项目之间反复重装。这个坑每个 Python 开发者都踩过一次。

```bash
uv venv                 # 创建 .venv/
uv pip install -e '.[dev]'
```

`.venv/` 进 `.gitignore`——它是**可以从声明重建出来的**，不属于源代码。

### 5.2 依赖分两类

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
> **`optional-dependencies`** 是按需的，`pip install '.[dev]'` 才会装。
>
> 分开的理由很实在：**用户不需要你的测试框架。** 混在一起，每个用户都得下载 pytest。

注意 `dependencies = []`——**运行时依赖是空的**。到第 12 章之前只用标准库。

每加一个依赖都是一次决策：多一个可能有漏洞的东西、多一个可能不兼容的东西、多一个
可能停止维护的东西。**能不加就不加**，这不是洁癖，是后面第 9 章讲 MCP 时会具体算的账。

最后那行值得单独说：

```toml
"tomli>=2.0; python_version < '3.11'",
```

> 分号后面是**环境标记（environment marker）**：只在满足条件时才装。
> `tomllib` 是 3.11 才进标准库的，旧版本需要 `tomli` 这个后向移植包。
> 有了标记，**一份依赖列表能同时对所有 Python 版本正确**，不用分叉文件。

### 5.3 版本号为什么都带 `>=`

```
"pytest>=8.0"
```

不写会怎样？工具会装当时能找到的最新版。今天是 pytest 9，三个月后可能是 10，
某个行为变了，**同一个 commit 在两天之间有了两种结果**。

`>=8.0` 是**下界**：低于这个版本我保证不行。但它不解决"今天和明天装的不一样"。

### 5.4 锁文件

```bash
uv lock
```

生成 `uv.lock`，**提交进仓库**。

> **锁文件**记录的是**这一次解析出来的每个包的确切版本和哈希**。
> `pyproject.toml` 说的是"我能接受什么范围"，锁文件说的是"这次实际装了什么"。

CI 里这样用：

```yaml
run: uv sync --frozen --all-extras
```

> `--frozen`：如果 `uv.lock` 和 `pyproject.toml` 对不上，**直接失败**，
> 不许自作主张重新解析。这样"本地和 CI 装的不一样"就从玄学变成一个红叉。

> **常见困惑**：库项目不是不该锁版本吗？
> 两回事。`pyproject.toml` 里的 `>=8.0` 是**给用户的兼容承诺**；
> `uv.lock` 是**给贡献者的复现保证**。前者宽，后者紧，互不冲突。

---

## §6 交给 git

第二条主线正式登场：**工程化的行为**。

### 6.1 `.gitignore` 先写

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

第二类有个性质值得记住：**它没有修复方案，只有止损方案。** 密钥一旦进了 git 历史，
`git rm` 没用——历史还在，别人的 clone 还在，fork 还在。正确流程是撤销那个密钥、
重写历史、通知所有人重新 clone。

所以只能预防，而预防的窗口就是现在，第一个 commit 之前。

### 6.2 第一个 commit 应该包含什么

**不是"等功能写完了再提交"。** 是现在——一个能装、能跑的骨架。

理由：commit 的价值是"一个可以回到的点"。骨架就是一个非常有用的可回到的点。

### 6.3 怎么切

本章切成四个：

```
835f366 chore: make minicodex installable as a package
7959ab1 test: prove the package installs, not just imports
575eeb1 fix: ship the prompt file inside the wheel
dad3dba ci: run install, lint and tests on every pull request
```

判断标准**不是大小，是能不能单独撤销**。撤掉 CI 不该把打包配置一起带走。

一个更实用的自检：**如果标题里非要用 "and"，那就是两个 commit。**

### 6.4 message 怎么写

格式用 **Conventional Commits**：`类型: 一句话`。类型常用这几个——
`feat`（新功能）、`fix`（修 bug）、`test`、`refactor`、`chore`（杂务）、`ci`、`docs`。

好处不是"看起来专业"，是**扫一眼 `git log` 就知道哪些是行为变更**。`refactor` 全是
安全的，`fix` 要重点看。

**标题写做了什么，body 写为什么。**

因为"做了什么" `git show` 一秒就答了。半年后没人会问那个。他们会问的是——

> 为什么源码非要放在 `src/` 底下？我能挪出来吗？

第一条 commit 的 body 就是在回答这个：

```
chore: make minicodex installable as a package

Goal for this step: `minicodex --version` works on a machine that has never
seen this repository, in three commands.

src layout rather than flat. With a flat layout the repo root is on sys.path,
so `import minicodex` succeeds whether or not the package was ever installed
-- which means packaging mistakes stay invisible until a user hits them.

[build-system] is declared explicitly. Omitting it makes every tool fall back
to setuptools, and the fallbacks are not identical, so the artefact depends on
which tool built it.
```

第三条更有意思，因为它记的是**一次真实的翻车**：

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

**它写清了这个 bug 是怎么被发现的。** 因为半年后有人嫌那个 0.3 秒的测试慢、想删掉的
时候，这段话就是保留它的理由。

**几个会让未来的你痛苦的写法**：

| 写法 | 问题 |
|---|---|
| `initial commit` | 每个仓库都有一个，没有一个有用 |
| `update` / `wip` | 你在给未来的自己出谜语 |
| `fix bug` | 版本控制史上最贵的四个字符 |
| `add src layout, ruff, tests and CI` | 一个 commit，四件事，一件都单独撤不掉 |

### 6.5 分支

本章直接提在 `main` 上。**这是全书唯一一次。**

理由：脚手架没有"行为"可以 review。从下一章开始，每个功能走一个 topic 分支：

```bash
git checkout -b feat/agent-loop
```

命名照抄 codex 的 `docs/contributing.md`：`feat/xxx`、`fix/xxx`。

> **为什么不用 git flow**（那套 develop/release/hotfix 分支）：它是为"同时维护多个
> 已发布版本"设计的。我们只有一条线。**流程的复杂度要匹配问题的复杂度**，
> 不匹配的那部分只会变成负担。

---

## §7 别人机器上也对吗

我只有一台电脑，而这台电脑上装了很多我忘了自己装过的东西。

### 7.1 先解决"这行代码该怎么排"

在配 CI 之前先加个格式化工具，理由不是审美：

**两个人的编辑器保存时格式化行为不同，于是每次 diff 里都混着一堆无关的行。**
review 的时候你要在 30 行变更里找出真正改了逻辑的那 2 行。

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
    "ASYNC",         # async footguns; nothing async yet, see chapter 0
    "RUF",
]
```

> **ruff 是什么**：一个二进制文件，同时干格式化（原来 black 干的）、
> 排 import（isort）、静态检查（flake8）、语法现代化（pyupgrade）。
> 一个工具、一份配置、一个版本要 pin。

`select` 里那些是**规则组**。前面几组是常规，`B` 和 `ASYNC` 值得单说：

- **`B`（bugbear）**：抓的是真 bug 不是风格。比如可变默认参数 `def f(x=[])`。
- **`ASYNC`**：异步的陷阱。**现在一行异步代码都没有，为什么打开？**
  因为下一章全是异步的，而这类错误的特点是**不报错、只是悄悄变慢**。
  下一章你会看到它在一个 bug 被写出来的那一秒就拦住了它。

格式化和检查是分开的两条命令：

```bash
ruff format .      # 排版
ruff check .       # 找问题
```

### 7.2 CI

> **CI（持续集成）**：你 push 代码时，一台干净的机器自动把你的项目从头装一遍、
> 跑一遍测试。它的价值就一句话：**在一台不是你的电脑上验证。**

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

> **`on:`** 什么时候跑。这里是"提 PR 时"和"推到 main 时"。
> **`jobs:`** 一组任务。`check` 是我们给它起的名字。
> **`runs-on:`** 在什么机器上跑。
> **`steps:`** 按顺序执行。`uses:` 是别人写好的动作，`run:` 是直接跑命令。
> **`timeout-minutes: 10`** 现在就加——一个卡住的 job 会堵住排在它后面的每一个 PR。

**三个步骤，跑完不到一分钟。** 明确没有的：平台矩阵、多 Python 版本、覆盖率门禁、
安全扫描、依赖审计。

这不是偷懒。这是一条明确的规则：

> **一个检查要进"能挡住合并"的 CI，前提是它拦下过真实的问题。**

反过来会发生什么，见过太多次了：项目还没功能，CI 已经 18 分钟；某个检查因为上游镜像
抽风间歇性失败；有人加了 `continue-on-error: true`；最后所有人学会了看见红叉直接
merge。

**一个被绕过的检查，保护效果精确等于零**，而且比没有检查更糟——它给了你安全感。

新检查的正当理由长这样："上周有个 bug 溜进 main，这个检查能拦住它。"
不是"最佳实践建议配置这个"。

> **这套 CI 之后会长成什么样**：到第 14 章，它会自然分成两层，和 codex 现在一样——
> `blocking-ci`（快，挡合并）和 `postmerge-ci`（慢，不挡合并，跑全平台矩阵）。
> codex 那个 `blocking-ci.yml` 现在有 7 个检查，**是三年长出来的。**

### 7.3 顺手：编辑器配置

`.vscode/` 三个文件跟着仓库走。不是强迫别人用 VS Code，是让用的人**看到和 CI 一样的
提示**，别等 CI 红了才知道。

`extensions.json` 首次打开时弹窗推荐插件；`settings.json` 让保存时自动格式化、
自动发现测试；`launch.json` 配调试。

调 Agent 时真正省时间的是这几个，现在知道有就行，第 0 章会真的用上：

- **`"justMyCode": false`** —— 默认不让你步进到库代码里。第一次遇到 asyncio 的怪
  行为时，你最需要的恰恰是步进进去。现在就关掉。
- **条件断点** —— Agent 的 bug 从来不在第 1 轮，在第 23 轮。断点上右键，填
  `turn_index == 23`。替代方案是加 print 重跑，一次两分钟。
- **`pytest -x --lf --pdb`** —— 第一个失败就停、只跑上次失败的、失败立刻进调试器。
  修 bug 时的默认姿势。

> **PyCharm 用户**：三条都有对应功能。`justMyCode` 对应
> Settings → Debugger → Python → Do not step into library scripts。

---

## §8 回头看：这一章顺手防住了什么

前面七节，我一次都没有说"现在我们来防范故障"。所有东西都是**从"我要达成这个目标"和
"我怎么知道成了"推出来的**。

但确实防住了不少。这是第三条主线的正确顺序：**先做事，再回头给踩过的坑编号存档。**

| 编号 | 故障 | 怎么暴露的 | 挡住它的东西 |
|---|---|---|---|
| F-1-01 | 测试跑的是源码目录，不是安装的包 | 🟡 卸载了包测试还是绿 | src layout + metadata 断言 + wheel 内容断言 |
| F-1-02 | 本地能跑 CI 挂（或反过来） | 🔴 CI 红本地绿 | 锁文件 + `--frozen` + 显式构建后端 |
| F-1-03 | 密钥进了仓库 | ⚫ 收到 GitHub 的告警邮件 | `.gitignore` 在第一个 commit 之前 |
| F-1-05 | 第一天配全套 CI，第二周关掉 CI | 🟣 发现所有人都在点 merge anyway | 三步起步，按证据增长 |

标记的含义（全书通用）：

🔴 崩溃 · 🟡 静默错误 · 🟢 主动边界测试 · 🔵 长跑才现 · 🟠 看日志发现 ·
🟣 review 发现 · ⚫ 用户报告 · ⚪ lint/类型检查

**注意 F-1-01 是 🟡。** 它不报错，测试还是绿的，一切看起来都好——直到用户装上。

这个比例会贯穿全书：**164 条故障里只有 23 条会自己报错。剩下 86% 需要你主动去撞。**

所以本章真正的产出，不是一个能装的包，是**三个能替你去撞的东西**：
一个不给白送 import 的目录结构、一个会打开 wheel 检查的测试、一台不是你的电脑。

> **上一版的这一章里有个 `recorder.py`**（把模型请求录到磁盘上）。它挪到第 0 章去了。
> 理由就是本章的方法论：**那时候还没有模型，没有"请求"这个东西，写它就是无源之水。**
> 需求驱动的意思是——**需求还没出现的时候，就不要动手。**

---

## §9 codex 是怎么做的

**`.vscode/` 同一套思路。** codex 的 `extensions.json` 推荐 rust-analyzer、ruff、
even-better-toml；`launch.json` 有 "Cargo launch" 和 **"Attach to running codex CLI"**。
注意第二个——**挂到一个已经在跑的进程上**，这是调长时间运行的 Agent 的常用手段，
Python 对应 `debugpy.listen()`。

**CI 是分层的。** `blocking-ci.yml` 顶上一行注释：

> This is the single entrypoint for checks that block a PR merge.

底下挂 7 个可复用 workflow。另一个入口 `postmerge-ci.yml` 注释写着
"intentionally outside the merge-blocking suite"，跑重的。

里面有一段特别值得看：

```yaml
  required:
    name: CI required
    # Without `always()`, GitHub skips this job after a failed dependency and a
    # required check can appear successful instead of reporting the failure.
    if: ${{ always() }}
```

这是一条吃过亏才知道的 GitHub Actions 陷阱：依赖失败时汇总 job 会被跳过，而"跳过"在
分支保护规则里可能被当成通过。**这条注释解释的不是代码在做什么，是为什么必须这么
做**——和我们对 commit message body 的要求完全一致。

**`AGENTS.md`。** codex 根目录有一份给 AI 看的项目规约。里面几条特别有考古价值：

> resist adding code to codex-core!

> Target Rust modules under 500 LoC, excluding tests. If a file exceeds roughly
> 800 LoC, add new functionality in a new module...

> Do not create small helper methods that are referenced only once.

**这不是设计文档，是护栏。护栏是撞过之后才装的。** 第一条甚至解释了自己的来历：core
已经变成最大的 crate，因为"往 core 里加"永远比"重构出新 crate"省事。

我们的规约会用同样的方式生长：**撞一次，加一条。**

---

## §10 三条线各自留下了什么

### 主线 A · 怎么把需求变成代码

**方法**：把目标里的每个词拆开问"这需要什么"，直到答案是具体动作。然后对每一项追问
两遍——**我怎么验证？** 和 **明天/别人那里还成吗？**

大部分工程化的东西都是这两个追问的答案。它们不是外部规范强加的，**是需求本来就有、
只是没说出口的部分。**

**顺序**：从"不做就没法验证下一步"的那个开始。

### 主线 B · 工程化的行为

| 动作 | 本章的规则 |
|---|---|
| 什么时候 commit | 一个能独立撤销的完整想法完成时 |
| message 怎么写 | 标题写做了什么，body 写**为什么**和**否决了什么** |
| 什么时候开分支 | 有行为变更就开；纯脚手架可以不开 |
| 什么时候加 CI 检查 | 它拦下过真实的问题才加 |
| `.gitignore` 什么时候写 | 第一个 commit 之前，因为密钥只能预防 |
| 锁文件要不要提交 | 要。它和依赖范围是两件事 |

### 主线 C · 故障的预防与发现

**本章最重要的一招**，会在全书反复出现：

> **把一个悄悄出错的场景，改造成一个大声报错的场景。**

src layout 没有"修好"任何 bug。它做的是让"没安装"这件事从**静默通过**变成
**collection error**。修的是一类问题的可见性，不是一个问题的实例。

**第二招**：

> 写完修复，花三十秒把修复改回去，确认测试会红。
> 一个对着坏代码也能通过的测试，是装饰品。

**第三招**（本章吃了亏才学到的）：

> 测你**交付**的东西，不是你**开发**的东西。
> 可编辑安装读源码目录，所以它对打包问题完全免疫——包括对 bug 免疫。

---

## 如果你只记住三件事

1. **`import` 找得到 ≠ 已安装。** 当前目录白送你一次 import，而这次白送会让你的测试
   对一整类打包问题免疫，直到用户装上才崩。

2. **需求做完之后要追问两遍**：我怎么验证它成了？它明天/在别人那里还成吗？
   测试、CI、锁文件都是这两个问题的答案，不是规范强加的负担。

3. **把静默失败改造成响亮失败。** 这比修好一个 bug 更值——你修的是一类问题的可见性。

---

## 动手

```bash
cd steps/step-1_setup
uv sync --all-extras
uv run minicodex --version
uv run pytest
```

预期输出：

```
minicodex 0.0.1
python    3.10.12 (linux)

..............                                                           [100%]
14 passed in 0.37s
```

**建议你自己做一遍的两件事**（十分钟，比读十页有用）：

1. 把 `[build-system]` 那段删掉，跑 `uv run pytest`。看哪个测试红了，读它的报错。
2. 把 `src/minicodex/` 挪到仓库根目录，改 `pyproject.toml` 让它能装上，然后
   `uv pip uninstall minicodex` 再跑测试。看看还剩几个测试能发现问题。

---

**下一章**：Ch00 · 最小 Agent 循环——写一个 while 循环，然后发现"模型说它读了文件"和
"文件真的被读了"是两件事。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 2 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。这一章正文已经
给了 `system_prompt()` 和 `main()` 的核心，附录只补两处正文没提的实现细节
（`__all__` 和 `_PROMPTS` 常量）。代码摘自
`steps/step-1_setup/src/minicodex/`，逐段核对过。

## T1 · `__init__.py` 完整实现

正文 §9 附近给了 `system_prompt()` 的函数体，但文件头部的 `__all__` 和
`_PROMPTS` 常量没进正文。完整文件：

```python
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

三个细节：

1. **`__all__` 是显式导出清单**——`from minicodex import *` 只拿到
   `__version__` 和 `system_prompt`。第 0 章录音里模型读了 `__init__.py`
   后回答 "defines `__version__` and `system_prompt`"，正是从这份清单读
   到的。
2. **`_PROMPTS = Path(__file__).parent / "prompts"` 是模块级常量**——相对于
   包目录定位 prompts 子目录（`Path(__file__).parent` 是 `__init__.py`
   所在目录），而不是相对于 cwd。这样无论从哪运行，`system.md` 都能找到。
   docstring 明说：**数据文件是证明打包工作的最便宜方式**——只 import .py
   的代码在 wheel 坏掉时也能过测试，而 `system_prompt()` 读文件就露馅。
3. **`__version__ = "0.0.1"` 是版本号的唯一来源**——第 15 章 `release.py`
   从这里 import（"一个版本号，一处"）。

## T2 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| `system_prompt()` 找不到 system.md | 用 `Path("prompts")` 相对 cwd | `Path(__file__).parent / "prompts"` 相对包目录 |
| wheel 装上后 `system_prompt` 报错 | 数据文件没进 wheel | 测试打开构建的 wheel 断言文件在（正文 F-1-01） |
| `from minicodex import *` 带进私货 | 没 `__all__` | 显式 `__all__ = ["__version__", "system_prompt"]` |
