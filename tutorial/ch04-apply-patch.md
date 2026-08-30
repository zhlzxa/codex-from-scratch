# 第 4 章 · 改文件

> **代码**：`steps/step04_apply_patch/`
> **分支**：`feat/apply-patch`
> **产出**：Agent 能改代码，而且改错的时候不会留下半个仓库
> **你需要**：Ollama（同前）。§4 那一节的对比来自两家真实模型，有 key 会更完整。

---

## §1 这一章要做出来的东西

前三章的 Agent 能读文件、能跑命令、能被正确调用。**它还不能改任何东西。**

改文件听起来比跑 shell 简单——`open(path, "w")` 就完了。实际上它是目前为止破坏力
最大的一个工具：跑错一条命令，输出是错的；改错一个文件，**代码库是错的**，而且
可能没人立刻发现。

---

## §2 先写一坨

最直接的写法：

```python
def apply_patch(path: Path, old_text: str, new_text: str) -> str:
    content = path.read_text(encoding="utf-8")
    if old_text not in content:
        return "Error: old_text not found"
    return content.replace(old_text, new_text)
```

拿一个真实形状的文件试：

```python
def clean_title(text: str) -> str:
    if not text:
        return ""
    return text.strip().title()


def clean_body(text: str) -> str:
    if not text:
        return ""
    return text.strip().lower()
```

改 `clean_body` 的最后一行：

```
>>> apply_patch(p, "    return text.strip().lower()", "    return text.strip()")
worked: True
```

能动。

---

## §3 撞的第一件事：改了一处，落地三处

现在要求改 `clean_body` 的空值保护，让它返回 `None`：

```
>>> out = apply_patch(p, '    if not text:\n        return ""',
...                      '    if not text:\n        return None')
>>> out.count("return None")
3
```

**三个函数的保护全被改了。** 而且返回值里没有任何提示——`str.replace()` 默认替换
所有匹配，它做完之后报告成功。

这不是边界情况，是 `str.replace()` 的定义。一个稍微通用一点的代码块（空值检查、
日志行、`return None`）在一个文件里出现两三次是常态。

> **第一条规则：歧义是错误，永远不是猜测。**
>
> 如果 `old_text` 匹配到多个位置，唯一正确的行为是拒绝并说明——因为**没有任何
> 信息**能告诉我们模型指的是哪一个。挑第一个、挑最后一个、挑最像的，都是在替
> 模型做一个它没做的决定。

---

## §4 那模型该怎么告诉我们改哪里？先去问它

有两种主流做法，选哪个不能拍脑袋：

- **unified diff**：`@@ -12,7 +12,9 @@` 加上下文行和 `+`/`-` 标记。git 用的就是它。
- **上下文锚定**：贴一段原文，说替换成什么。不要行号。

第一种看起来更"标准"。但它要求模型**做算术**——数出这一段从第几行开始、有几行。
模型能不能做对，只能测。

同一个编辑任务，同一个文件，两种格式，两家模型各采样三次。

### 4.1 要求 unified diff

```
Ollama (gemma4:31b-cloud):
  1: [1 hunk] @@ says 20, that line is at [17]; @@ -20,4 runs past EOF (21 lines)
  2: [1 hunk] headers plausible
  3: [1 hunk] headers plausible

OpenAI (gpt-5.4-nano):
  1: [NO @@ HEADER AT ALL] '*** Begin Patch\n*** Update File: src/textutil.py\n@@\n def clean_body...'
  2: [NO @@ HEADER AT ALL] '*** Begin Patch\n*** Update File: src/textutil.py\n@@\n def clean_body...'
  3: [1 hunk] headers plausible
```

Ollama 三次里错一次——而且错得很彻底：说是第 20 行，实际在第 17 行，声明的范围
还超出了文件末尾。**如果照着这个行号去切文件，切到的是别人的代码。**

OpenAI 那两次更有意思。它没有拒绝，也没有算错——**它换了个格式**：

```
*** Begin Patch
*** Update File: src/textutil.py
@@
 def clean_body(text: str) -> str:
```

这是 codex 自己的 patch 格式。注意那个 `@@` **后面是空的**——没有行号，下面直接
跟上下文行。

> 这个发现值两件事：
>
> 1. **这个格式已经进了模型的训练数据。** 我要求 unified diff，它给我 codex 的格式，
>    三次里两次。
> 2. **而这个格式恰恰不含行号。** 一个被大量 patch 数据训练过的模型，自己选的表示法
>    里没有算术——这是对"别让模型数行"最有力的一票，而且不是我投的。

### 4.2 要求上下文锚定

同一个任务，工具改成 `old_text` / `new_text`，描述里写清楚"必须在文件里恰好出现
一次，不唯一就把周围的行也包进来"：

```
Ollama:  1-3: [EXACT, unique] 'return text.strip().lower()'
OpenAI:  1-3: [EXACT, unique] 'def clean_body(text: str) -> str:'
```

**两家，六次，全部精确唯一匹配，一个字符都不差。**

有意思的是两家选的锚点不同：Ollama 贴的是要改的那一行，OpenAI 贴的是函数签名行。
都对——都是文件里唯一的。

### 4.3 歧义的时候呢

把任务换成 §3 那个：改 `clean_body` 的空值保护——那个块在文件里一模一样出现了三次。

```
Ollama:  1-3: [unique -- widened to include clean_body]
OpenAI:  1-3: [unique -- widened to include clean_body]
```

**两家 3/3 都主动把函数签名行包了进来**，让锚点变唯一。

这一句描述起了作用：

```
If the text you want to change is not unique, include surrounding lines until it is.
```

> **这和第 3 章的结论不矛盾，两者合起来才完整。**
>
> 第 3 章测到：描述里写"命令超过 30 秒会被杀"，模型 6/6 照样发出两分钟的命令——
> **无效**。
>
> 这一章测到：描述里写"锚点不唯一就扩大范围"，模型 6/6 照做——**有效**。
>
> 区别在于约束的对象。**前者要求模型改变它对外部世界的预期**（它没法让测试跑得
> 更快）；**后者只要求模型改变自己输出的形状**——那完全在它控制之内。
>
> 写描述之前先问一句：我要求的这件事，模型自己做得到吗？

### 4.4 顺便测的一件事，以及它为什么不算数

还测了"只给一个整文件写入工具，模型会不会漏掉东西"。结果是两家都完好无损地交回了
整个文件，只改了该改的地方（342 字符变成 334，正好是删掉的 `.lower()` 那 8 个字符）。

**但这个结果不能用来下结论。** 目标文件只有 20 行——20 行的文件模型可以完整背下来。
真正的风险在几百行的文件上，而我没测那个。

在这里如实记一笔：**这一章选择局部编辑而不是整文件重写，理由是 token 成本和
§4.1 的行号问题，不是因为我测出了整文件重写会丢代码。** 那个我没测出来。

---

## §5 定位：分三级，每一级都要唯一

模型贴回来的 `old_text` 有三种情况，实测都见过：

1. 和文件里逐字节相同 —— 最常见
2. 内容对，尾随空白不同 —— 模型抄的时候会规整化
3. 内容对，缩进不同 —— 模型从嵌套上下文里抄出来时会重新缩进

一级一级放宽，**但每一级都必须只找到一个位置**：

```python
_LEVELS = (
    ("exactly", _exact),
    ("ignoring trailing whitespace", _trailing_insensitive),
    ("ignoring indentation", _indentation_insensitive),
)


def locate(content: str, old: str) -> tuple[tuple[int, int] | None, str | None]:
    """(span, None) on a unique hit, else (None, why it failed)."""
    for how, find in _LEVELS:
        spans = find(content, old)
        if len(spans) == 1:
            return spans[0], None
        if len(spans) > 1:
            lines = [content[:s].count("\n") + 1 for s, _ in spans]
            return None, (
                f"that text appears {len(spans)} times (matching {how}), on lines "
                f"{', '.join(map(str, lines))}"
            )
    return None, "that text is not in the file"
```

**错误信息里带行号**，这是第 3 章那条实测结论的直接应用：错误信息是 prompt，
"它出现了两次"让模型无从下手，"它出现在第 5 行和第 11 行"能让模型知道该往哪边扩。

三个匹配函数：

```python
def _exact(content: str, old: str) -> list[tuple[int, int]]:
    spans, i = [], content.find(old)
    while i != -1:
        spans.append((i, i + len(old)))
        i = content.find(old, i + 1)
    return spans


def _trailing_insensitive(content: str, old: str) -> list[tuple[int, int]]:
    """Same text, but trailing whitespace on each line need not agree."""
    pattern = "\n".join(re.escape(ln.rstrip()) + r"[ \t]*" for ln in old.split("\n"))
    return [(m.start(), m.end()) for m in re.finditer(pattern, content)]


def _indentation_insensitive(content: str, old: str) -> list[tuple[int, int]]:
    """Last resort: the lines are right, the indentation is not."""
    parts = [re.escape(ln.strip()) for ln in old.split("\n") if ln.strip()]
    if not parts:
        return []
    pattern = r"[ \t]*" + r"[ \t]*\n[ \t]*".join(parts) + r"[ \t]*"
    return [(m.start(), m.end()) for m in re.finditer(pattern, content)]
```

### 5.1 顺序是有意义的，而我差点没测到

先试精确、再放宽，看起来是显然的。**但"显然"不等于"被测过"**——§11.2 会讲，
把这三级的顺序倒过来，当时全部 124 个测试仍然是绿的。

补测试的时候还撞出一件事：

```
content = "def a():\n    return 1\n\ndef b():\n        return 1\n"
old     = "    return 1"          # 四个空格

exact hits: [(9, 21), (36, 48)]   # 两个
```

八个空格缩进的那一行，**包含**四个空格的锚点作为子串。所以连精确匹配都报了歧义。

> **纯子串匹配在嵌套缩进面前，自己就是有歧义的。** 这不是 bug——歧义被正确地报告了——
> 但它意味着"精确匹配肯定唯一"这个直觉是错的。

---

## §6 撞的第二件事：一行编辑，整个文件的行尾被重写

一个从 Windows 检出的文件：

```python
>>> path.write_bytes(b'def f():\r\n    return 1\r\n\r\ndef g():\r\n    return 2\r\n')
>>> content = path.read_text(encoding="utf-8")
>>> content
'def f():\n    return 1\n\ndef g():\n    return 2\n'
```

`read_text()` 默认开启 universal newlines，**CRLF 在读的时候就被转成了 LF**。所以
模型贴回来的 `\n` 匹配得非常顺利——这一步没有任何报错。

问题在写回去的时候：

```
before: b'def f():\r\n    return 1\r\n\r\ndef g():\r\n    return 2\r\n'
after:  b'def f():\n    return 99\n\ndef g():\n    return 2\n'

CRLF count before: 5   after: 0
```

**要求改一行，文件里每一个行尾都被改掉了。** 工具输出里看不出任何异常，git diff
里整个文件都是红绿。

修法是读的时候不让 Python 翻译，自己记住原来是什么：

```python
def read_source(path: Path) -> tuple[str, str]:
    """(text with LF endings, the ending the file actually uses).

    `newline=""` stops Python translating on read, which is what makes it
    possible to notice CRLF at all. `Path.read_text()` grew a `newline`
    parameter in 3.13; this has to work on 3.10, so it uses `open`.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    if "\r\n" in raw:
        return raw.replace("\r\n", "\n"), "\r\n"
    return raw, "\n"


def write_source(path: Path, text: str, ending: str) -> None:
    if ending != "\n":
        text = text.replace("\n", ending)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
```

**归一化进来，恢复回去。** 内部一律用 `\n` 做匹配（模型看到的也是 `\n`），落盘时
还原成文件本来的样子。

> 顺带一个版本坑：`Path.read_text()` 的 `newline` 参数是 3.13 才加的。这个项目
> 声明支持 3.10，所以只能用 `open()`。**这类东西查文档不如跑一行。**

一个诚实的限制：**行尾混用的文件没有"本来的样子"可以还原**。

```
before: b'a\r\nb\nc\r\n'
after:  b'a\r\nb\r\nc\r\n'      ← 那个单独的 LF 也被转成了 CRLF
```

检测到有 CRLF 就整体按 CRLF 还原，混用文件会被统一。这个不修——**没有正确答案，
只有取舍**，写进注释和测试里。

---

## §7 撞的第三件事：五个编辑，第三个失败

一个 patch 改五个文件，第三个的锚点对不上：

```
=== naive: write as you go ===
  f1.py: written
  f2.py: written
  f3.py: FAILED -- stopping here

state on disk now:
  f1.py: VALUE = 100      ← 改了
  f2.py: VALUE = 200      ← 改了
  f3.py: VALUE = 3
  f4.py: VALUE = 4
  f5.py: VALUE = 5
```

**仓库现在处在一个模型从没要求过的状态里**，而且模型不知道——它收到的只是一句
"第三个失败了"。它下一步可能重发整个 patch（于是 f1、f2 的锚点也对不上了），
也可能以为什么都没发生。

修法是把校验和写入分成两段：

```python
    planned: list[tuple[Path, str, str]] = []
    for index, edit in enumerate(edits, 1):
        ...                       # 解析路径、定位、检查语法
        planned.append((path, updated, ending))

    for path, text, ending in planned:
        write_source(path, text, ending)
```

**全部通过才动第一个字节。** 失败的时候磁盘上什么都没变，模型收到一条错误，
重发整个 patch 是安全的。

---

## §8 撞的第四件事：第 3 章留下的一个洞

`apply_patch` 要防止编辑跑到仓库外面去。第 3 章已经写了路径解析，直接复用：

```python
path, error = resolve(edit.path, root)
```

顺手测一下：

```
'../../../../etc/passwd'   ->  OK: /etc/passwd
```

**通过了。**

第 3 章的 `resolve()` 是这么写的：

```python
if candidate.is_absolute():
    try:
        relative = candidate.resolve().relative_to(root)
    except ValueError:
        return None, tool_error("that path is outside the repository", ...)
    candidate = relative
```

越界检查**只在绝对路径这个分支里**。`../../../../etc/passwd` 不是绝对路径，
所以那段代码根本没跑。

而第 3 章的测试是这么写的：

```python
def test_F03_02_an_absolute_path_outside_the_repo_is_refused(tmp_path: Path) -> None:
    got, error = resolve("/etc/hosts", tmp_path)
```

**用的是绝对路径。** 测过的分支就是能工作的分支，相对路径的逃逸从来没有被执行过。
整整一章，98 个测试全绿。

> **你的测试证明的是你测过的东西，不是你以为的东西。**
>
> 这个洞不是"忘了写检查"——检查写了，测试也写了，两者还互相印证。是**检查和测试
> 犯了同一个错误**：都默认了"越界"等于"绝对路径"。

修法是不再区分两种拼法：

```python
    # Resolve first, judge second. Chapter 3 branched on `is_absolute()` and
    # only checked containment inside that branch, so `../../../../etc/passwd`
    # went straight through -- it is not absolute, and nothing else looked.
    full = (root / candidate).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        return None, tool_error(
            "that path is outside the repository, and this tool only touches files inside it",
            you_sent=raw,
            do_this=f"Send a path relative to {root.name}/, for example src/minicodex/model.py",
        )
```

**先解析，再判断落在哪。** 绝对路径越界和相对路径逃逸本来就是同一个问题问两遍，
合并之后代码还短了——`is_absolute()` 那个分支整个删掉了。

注意 `src/../a.py` 仍然是合法的：它解析之后还在仓库里。**一刀切禁掉 `..` 会更简单，
也会误伤。**

---

## §9 撞的第五件事：编辑完了，文件还能不能用

匹配成功、写入成功，不代表结果是对的：

```python
>>> ast.parse('def f():\nreturn 1\n')
SyntaxError: expected an indented block after function definition on line 1
```

模型抄错缩进、少个括号，文件就废了。而 Agent 会继续跑，直到某个测试因为
`SyntaxError` 挂掉——那时候已经隔了好几轮。

写完之前先解析一遍：

```python
def syntax_error(path: Path, text: str) -> str | None:
    """A parse error, for the file types we can parse. `None` otherwise."""
    if path.suffix != ".py":
        return None
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return f"{exc.msg} at line {exc.lineno}"
    return None
```

这个检查放在**写入之前**，所以失败时文件是干净的：

```
Error: textutil.py: the edit would leave the file unparseable: expected ':' at line 10
Nothing has been written. Re-read it and send an edit that leaves it syntactically valid.
```

只对 `.py` 生效。`.md`、`.json`、`.rs` 照写不误——**我们没有它们的解析器，
而"不检查"比"不让编辑"好得多**。

---

## §10 完整代码

```python
"""Applying an edit the model described, to a file it cannot see while it types."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from minicodex.paths import resolve
from minicodex.tool_errors import tool_error


@dataclass(frozen=True)
class Edit:
    """One replacement, as the model described it."""

    path: str
    old_text: str
    new_text: str


def read_source(path: Path) -> tuple[str, str]:
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    if "\r\n" in raw:
        return raw.replace("\r\n", "\n"), "\r\n"
    return raw, "\n"


def write_source(path: Path, text: str, ending: str) -> None:
    if ending != "\n":
        text = text.replace("\n", ending)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _exact(content: str, old: str) -> list[tuple[int, int]]:
    spans, i = [], content.find(old)
    while i != -1:
        spans.append((i, i + len(old)))
        i = content.find(old, i + 1)
    return spans


def _trailing_insensitive(content: str, old: str) -> list[tuple[int, int]]:
    pattern = "\n".join(re.escape(ln.rstrip()) + r"[ \t]*" for ln in old.split("\n"))
    return [(m.start(), m.end()) for m in re.finditer(pattern, content)]


def _indentation_insensitive(content: str, old: str) -> list[tuple[int, int]]:
    parts = [re.escape(ln.strip()) for ln in old.split("\n") if ln.strip()]
    if not parts:
        return []
    pattern = r"[ \t]*" + r"[ \t]*\n[ \t]*".join(parts) + r"[ \t]*"
    return [(m.start(), m.end()) for m in re.finditer(pattern, content)]


_LEVELS = (
    ("exactly", _exact),
    ("ignoring trailing whitespace", _trailing_insensitive),
    ("ignoring indentation", _indentation_insensitive),
)


def locate(content: str, old: str) -> tuple[tuple[int, int] | None, str | None]:
    for how, find in _LEVELS:
        spans = find(content, old)
        if len(spans) == 1:
            return spans[0], None
        if len(spans) > 1:
            lines = [content[:s].count("\n") + 1 for s, _ in spans]
            return None, (
                f"that text appears {len(spans)} times (matching {how}), on lines "
                f"{', '.join(map(str, lines))}"
            )
    return None, "that text is not in the file"


def syntax_error(path: Path, text: str) -> str | None:
    if path.suffix != ".py":
        return None
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return f"{exc.msg} at line {exc.lineno}"
    return None


def apply_edits(edits: list[Edit], root: Path) -> str:
    """Validate every edit, then write every edit, or write nothing."""
    if not edits:
        return tool_error(
            "the patch contained no edits",
            do_this="Send at least one edit with path, old_text and new_text.",
        )

    planned: list[tuple[Path, str, str]] = []
    for index, edit in enumerate(edits, 1):
        where = f"edit {index} of {len(edits)} ({edit.path})" if len(edits) > 1 else edit.path

        path, error = resolve(edit.path, root)
        if error is not None:
            return error
        assert path is not None

        content, ending = read_source(path)
        span, why = locate(content, edit.old_text)
        if span is None:
            return tool_error(
                f"{where}: {why}",
                you_sent=edit.old_text,
                do_this=(
                    "Re-read the file and copy the text to replace exactly as it "
                    "appears, including enough surrounding lines to make it unique."
                ),
            )

        start, end = span
        updated = content[:start] + edit.new_text + content[end:]

        broken = syntax_error(path, updated)
        if broken is not None:
            return tool_error(
                f"{where}: the edit would leave the file unparseable: {broken}",
                do_this=(
                    "Nothing has been written. Re-read the file and send an edit "
                    "that leaves it syntactically valid."
                ),
            )

        planned.append((path, updated, ending))

    for path, text, ending in planned:
        write_source(path, text, ending)

    names = ", ".join(sorted({e.path for e in edits}))
    return f"Applied {len(edits)} edit(s) to {names}."
```

工具层的包装和 schema 在交付代码的 `tools.py` 里。schema 里最重要的是这一句——
§4.3 实测证明它有效：

```
The text to replace, copied from the file. It must appear exactly once --
include whole surrounding lines until it does. Do not write line numbers or
diff markers.
```

---

## §11 验证

### 11.1 描述快照第一次真正干活

第 3 章写的那个"改描述就要同步改测试"的快照测试，加 `apply_patch` 的时候立刻红了。
**这正是它的设计意图。**

但它同时暴露了自己的一个缺口：收集逻辑只走了 `properties` 的第一层，而
`apply_patch` 的参数是一个**对象数组**——`path` / `old_text` / `new_text` 的描述
根本没被收集到，**包括那句承载全部唯一性要求的话**。

```python
def collect_descriptions() -> dict[str, str]:
    """Every description the model is shown, including nested ones.

    The first version of this walked only `properties`, one level deep. When
    chapter 4 added `apply_patch`, whose `edits` parameter is an array of
    objects, the descriptions of `path`, `old_text` and `new_text` were not
    collected at all -- including the sentence that carries the whole
    uniqueness requirement. A snapshot that does not reach the text is a
    snapshot of nothing.
    """
```

### 11.2 变异测试，以及它找到的那个缺口

| 撤销的修复 | 结果 |
|---|---|
| 歧义时取第一个匹配 | 红 ×3 |
| 去掉行尾保留 | 红 ×2 |
| 边校验边写入 | 红 |
| 去掉语法检查 | 红 ×3 |
| 路径检查改回第 3 章的写法 | 红 ×3（`/etc/passwd` 那条仍然绿——**精确复现了那个洞**） |
| **把三级匹配的顺序倒过来** | **全绿** ← |

最后一行是这次变异测试真正的收获：**顺序从来没有被测过**。

补的测试要构造一个"精确匹配唯一、宽松匹配多个"的文件。第一版用两处空格缩进，
结果发现精确匹配自己就报歧义（§5.1 那个子串问题）。改用 Tab：

```python
    content = "def a():\n    return 1\n\ndef b():\n\treturn 1\n"

    span, why = locate(content, "    return 1")

    assert why is None, "the exact, unique match should have been taken"
```

顺序倒过来之后：

```
assert 'that text appears 2 times (matching ignoring indentation), on lines 2, 5' is None
```

> 变异测试的价值不只是"确认测试有效"，**它会告诉你哪些代码路径从来没被测过。**
> 一个测试都不红的变异，说明那行代码可以随便改。

### 11.3 一次 lint 的自我证明

CJK 测试里的中文全角逗号触发了 `RUF001`——这条规则专门抓"长得像 ASCII 但不是"的
字符，比如混进英文标识符里的西里尔字母 `а`。中文注释里全角逗号是正确的，加
`noqa` 并写清理由。

然后在写那段理由的时候，我举例说"比如混进英文标识符里的西里尔字母 а"——
**真的打了一个西里尔字母**，被 `RUF002` 当场抓住。

这段留在代码里了：

```python
    The `noqa: RUF002` on this docstring is not a second exception to the same
    rule so much as a demonstration of it: writing the sentence above put a
    real Cyrillic `а` in this file, and ruff caught it. The rule earned its
    keep on the paragraph explaining why it was being suppressed.
```

> **绕过 lint 必须留下理由。** 而这次的理由本身，成了这条规则值得存在的证据。

### 11.4 全量

```
125 passed
ruff check: All checks passed!
ruff format --check: 24 files already formatted
```

---

## §12 文件清点

| 文件 | 出现位置 | 状态 |
|---|---|---|
| `src/minicodex/patch.py` | §10（完整） | ✅ |
| `src/minicodex/paths.py` | §8（改动部分），其余同第 3 章 | ✅ |
| `src/minicodex/tools.py` | §10 结尾（schema 关键句），完整版在交付代码 | ✅ |
| `tests/test_patch.py` | §5.1 / §11.2 / §11.3（关键片段），27 个测试完整版在交付代码 | ✅ |
| `tests/test_schemas.py` | §11.1（改动部分） | ✅ |
| `probe_editing.py` | §4（结果），非项目代码 | ✅ |

---

## §13 收工：commit 与 review

```
b8a3d3b test: cover the edit paths, including the one mutation testing found
a99e0cb feat: edit files by replacing exact blocks, all or nothing
486bfba fix: a relative path could climb out of the repository
```

**路径洞单独一个 commit，排在功能前面。** 它不是这个功能的一部分，是一个既有 bug——
混在 800 行的功能 diff 里，review 的人不会注意到那 5 行才是最重要的。

> 这是第 -1 章那条"先重构再加功能，两件事不混在一个 PR"的变体：
> **先修 bug 再加功能。** 而且修 bug 的那个 commit 可以单独 cherry-pick 回去。

### Code review

**1 · `apply_edits` 对同一个文件的两处编辑，行为是什么？**

> 作者：最后一个赢——每个编辑都是独立地对着磁盘上的内容做计划的，两个都算完了才写，
> 所以后写的覆盖了先写的。**这是个真 bug，但我没修**，因为实测里没有任何一次看到
> 模型给同一个文件发两个编辑。在见到真实案例之前定合并语义，就是在凭空发明规格。
> 写进测试注释了，测试名字就叫 `test_several_edits_to_one_file_all_land`，断言的是
> 当前这个（有缺陷的）行为——**下次有人改它，测试会红，那时候就有真实案例了**。

**2 · `_indentation_insensitive` 把空行都丢掉了，会不会匹配到跨越空行的位置？**

> 作者：会。这是故意的取舍——模型抄多行块的时候经常吞掉或加上空行。代价是
> 匹配可能跨过一个本不该跨的空行。**它是最后一级**，前两级都失败才会走到，
> 而且仍然要求唯一。真出问题的时候会是一个很难查的 bug，这条记在这里。

**3 · 语法检查只支持 Python，那 `.js`、`.rs` 呢？**

> 作者：不检查。加解析器意味着加依赖，而且每种语言一个。**目前只有 Python 是
> 我们自己的代码**，别的语言的文件在这个项目里是数据不是代码。第二种语言出现时
> 再说（三次法则：这是第一次）。

**4 · `read_source` 用 `open()` 而不是 `Path.read_text()`，不一致。**

> 作者：`Path.read_text()` 的 `newline` 参数 3.13 才有，项目声明支持 3.10。
> 这个理由写进 docstring 了——**不写的话，下一个人一定会"顺手统一"回去**，
> 然后 §6 那个 bug 就回来了。

**5 · probe 的 A 场景没能证明整文件重写会丢代码，但这一章还是选了局部编辑。**

> 作者：对，这条意见是对的，我在 §4.4 里如实写了。**局部编辑的理由是 token 成本和
> §4.1 的行号问题，不是"整文件重写会丢代码"** ——后者我没测出来，因为目标文件
> 只有 20 行。真要论证它，得拿几百行的文件重跑。**记着这笔账。**

---

## §14 codex 是怎么做的

**格式几乎一样。** codex 的 `apply_patch` 用的是
`*** Begin Patch` / `*** Update File:` / `@@` 的自定义文本格式，其中 `@@` 后面
**不跟行号**，靠上下文行定位。§4.1 里 gpt-5.4-nano 自发输出的就是这个格式。

**为什么是自定义文本而不是 JSON 结构**：多行代码穿过 JSON 字符串要转义，
第 3 章测过转义的三种命运——正确、双重转义（静默产出坏文件）、未转义（解析失败）。
codex 的选择是让模型少转义一层。这一章仍然用 JSON 参数（`old_text`/`new_text`），
因为实测两家都没有出现转义错误；**这是个可以被数据推翻的决定，不是原则**。

**容错匹配是分级的。** `codex-rs/apply-patch/` 里同样是先精确、再放宽空白，
理由和 §5 一样。

**歧义同样是错误。** 不猜。

---

## §15 回头看：这一章撞到了什么

| 故障 | 怎么暴露的 | 挡住它的东西 |
|---|---|---|
| `str.replace()` 改一处落地三处 | 🟡 静默（报告成功） | 歧义即错误，并报出行号 |
| 模型算错 `@@` 行号（说 20，实际 17，超出 EOF） | 🟢 A/B 实测 | 不要行号，改用上下文锚定 |
| 模型抄回来的缩进/尾随空白和文件不一致 | 🔵 长跑 | 三级匹配，每级都要唯一 |
| 精确子串匹配在嵌套缩进下自己就有歧义 | 🟢 写测试时发现 | 不修（歧义被正确报告），记录 |
| CRLF 文件被一行编辑重写全部行尾 | 🟡 静默（工具输出正常，diff 全红） | 归一化进、恢复出 |
| 行尾混用的文件没有"原样"可还原 | 🟢 边界测试 | 不修，记录取舍 |
| 五个编辑失败在第三个，前两个已落盘 | 🟢 边界测试 | 先全部校验，再全部写入 |
| **相对路径 `../..` 逃出仓库**（第 3 章遗留） | 🟢 复用旧代码时顺手测到 | 先解析再判断，删掉 `is_absolute()` 分支 |
| 编辑后文件语法坏掉，几轮之后才炸 | 🔵 长跑 | 写入前 `ast.parse` |
| 描述快照够不到嵌套 schema 的描述 | 🟣 加新工具时红了才发现 | 递归收集 |
| **三级匹配的顺序从未被测过** | ⚪ 变异测试 | 补一个用 Tab 构造的用例 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 review · ⚫ 用户报告 · ⚪ lint/类型

**这一章有两条故障不是来自新代码**：一条是第 3 章留下的安全洞，一条是第 3 章那个
快照测试自己的缺口。两条都是在**复用**旧代码的时候撞出来的。

> 新功能最好的测试，往往是**拿旧代码去干一件它没干过的事**。

---

## 如果你只记住三件事

1. **歧义是错误，不是猜测。** 匹配到多个位置时，没有任何信息能告诉你模型指的是哪个——
   挑一个就是替它做了一个它没做的决定，而且不会有人发现。
2. **别让模型做算术。** 实测：要求行号，一家算错、一家索性换成不带行号的格式；
   要求贴原文，两家六次全对。**你要它做的事，得是它擅长的事。**
3. **描述能约束模型的输出，约束不了模型对世界的预期。** "锚点不唯一就扩大范围"
   6/6 有效；第 3 章那句"命令超过 30 秒会被杀"6/6 无效。
   **区别在于这件事是不是它自己做得到。**

---

## 动手练习

1. 把 `_LEVELS` 的顺序改成先宽松后精确，跑测试。只有一个测试会红——找到它，
   读懂它为什么是唯一一个能发现这件事的。**然后想想你自己的项目里，有多少行代码
   处在"改了没人知道"的状态。**
2. 给同一个文件发两个编辑（`test_several_edits_to_one_file_all_land` 那个场景），
   把行为改成"依次叠加"而不是"最后一个赢"。你会需要在计划阶段就把前一个编辑的
   结果传给后一个——**做完之后回头看 review 第 1 条，判断这个改动现在该不该合入。**
3. 让 `apply_patch` 支持新建文件（`old_text` 为空表示创建）。注意 `resolve()`
   现在要求文件必须存在——**想清楚是改 `resolve()` 还是在 `apply_edits` 里绕过它，
   以及这个决定会不会把第 3 章的路径检查又撕开一个口子。**

下一章：Ch05 · 审批与沙箱——现在 Agent 能改任何文件、跑任何命令了。
包括 `rm -rf`。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 3 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`asyncio.to_thread`、`functools.partial`、`Path` 基础），
这里只讲这一章新出现的、容易让新手卡住的写法。代码摘自
`steps/step04_apply_patch/src/minicodex/`，逐段核对过。

先把范围说死：

1. 本附录只解释第 4 章在 `steps/step04_apply_patch/` 里新增或修改的代码。
   正文 §10 已经给了 `patch.py` 的完整代码（`Edit`/`read_source`/
   `write_source`/`_exact`/`_trailing_insensitive`/`_indentation_insensitive`/
   `_LEVELS`/`locate`/`syntax_error`/`apply_edits`），**不再重复**。这里补
   正文 §12 清点表说"完整版在交付代码"的：`tools.py` 的 `apply_patch`
   handler 和 `default_tools`/`tool_schemas` 完整版，以及 `paths.py` 的
   `resolve` 改动（§8 只给了改动片段）。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。

## N1 · `paths.py` 的 `resolve`：第 4 章的路径洞

正文 §8 说"一个相对路径能爬出仓库"是独立 commit。第 3 章的 `resolve`
只在**绝对路径**分支里检查越界，`../../../../etc/passwd` 这种相对路径
直接漏过去（它不是绝对路径，没有别的检查）。第 4 章改成"先解析、后判定"：

```python
    root = root.resolve()
    candidate = Path(raw)

    # Resolve first, judge second. Chapter 3 branched on `is_absolute()` and
    # only checked containment inside that branch, so `../../../../etc/passwd`
    # went straight through -- it is not absolute, and nothing else looked.
    # An absolute path inside the repository and a relative one that climbs out
    # are the same question asked twice; resolving first answers both.
    full = (root / candidate).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        return None, tool_error(
            "that path is outside the repository, and this tool only touches files inside it",
            you_sent=raw,
            do_this=f"Send a path relative to {root.name}/, for example src/minicodex/model.py",
        )
```

三个关键差异（对照第 3 章附录 M1 的版本）：

1. **`full = (root / candidate).resolve()` 先拼再解析**——`(root /
   candidate)` 把相对路径挂在 root 下，`.resolve()` 展开 `..` 和符号链接，
   得到"这个路径实际指向哪"。`../../../../etc/passwd` 解析完就是
   `/etc/passwd`，和 root 一比较立刻暴露。
2. **`relative_to(root)` 越界检查现在是无条件的**，不再分绝对/相对。
   docstring 注释说得清楚："仓库内的绝对路径和爬出去的相对路径是同一个
   问题问了两遍，先解析再判定两个都答了。" 第 3 章的 `is_absolute()`
   分支整个删掉了。
3. **错误消息从"this tool only reads files inside it"变成"only touches
   files"**——因为 `apply_patch` 也会写文件，"reads"是撒谎。

## N2 · `tools.py` 的 `apply_patch` handler

正文 §10 结尾只给了 schema 的关键句，handler 完整实现：

```python
async def apply_patch(root: Path, args: dict[str, Any]) -> str:
    """Replace an exact block of text in one or more files.

    The `edits` list is validated in full before anything is written --
    measured in chapter 4: writing as it goes leaves the repository
    half-edited when the third of five hunks does not match.
    """
    raw = args.get("edits")
    if not isinstance(raw, list):
        return tool_error(
            'apply_patch needs an "edits" argument, a list of objects',
            you_sent=repr(args.get("edits")),
            do_this=(
                'Example: {"edits": [{"path": "src/a.py", '
                '"old_text": "    return 1", "new_text": "    return 2"}]}'
            ),
        )

    edits: list[Edit] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return tool_error(
                f"edit {index} is not an object",
                you_sent=repr(item),
                do_this='Each edit needs "path", "old_text" and "new_text".',
            )
        missing = [k for k in ("path", "old_text", "new_text") if not isinstance(item.get(k), str)]
        if missing:
            return tool_error(
                f"edit {index} is missing {', '.join(missing)}",
                you_sent=repr(item)[:200],
                do_this='Each edit needs "path", "old_text" and "new_text", all strings.',
            )
        edits.append(Edit(item["path"], item["old_text"], item["new_text"]))

    return await asyncio.to_thread(apply_edits, edits, root)
```

四个新手容易漏的点：

1. **先整体校验，再调用 `apply_edits`。** handler 负责把"模型发的 JSON"
   变成"类型正确的 `Edit` 列表"（参数类型、缺失字段逐项检查，错误消息
   点名第几个 edit 缺什么），`apply_edits` 负责"定位、语法检查、全有或全无
   写入"。**分层：handler 管格式，`patch.py` 管语义。**
2. **`enumerate(raw, 1)` 从 1 开始编号**——错误消息说 "edit 1 of 3
   (src/a.py)"，人读的序号从 1 数起。
3. **`missing = [k for k in (...)]` 列出所有缺失字段**，而不是只报第一个
   ——一次告诉模型缺哪几个，少一轮往返。
4. **`await asyncio.to_thread(apply_edits, edits, root)`**——`apply_edits`
   是同步阻塞 IO（读文件、写文件），丢线程池。第 1 章 F00-09 的规则
   第三次应用。

`default_tools` 的完整版（比第 3 章多一行）：

```python
def default_tools(root: Path | None = None) -> dict[str, Any]:
    root = (root or Path.cwd()).resolve()
    session = ShellSession()
    return {
        "read_file": functools.partial(read_file, root),
        "run_shell": functools.partial(_run_shell, session),
        "apply_patch": functools.partial(apply_patch, root),
    }
```

`apply_patch` 和 `read_file` 一样绑 `root`（路径合法性归它管），
`run_shell` 绑 `session`（cwd/env 归它管）——每个工具绑"它真正需要的那
个每会话对象"。

## N3 · `tool_schemas` 里 `apply_patch` 的描述

正文 §10 结尾只给了 schema 里最有信息量的一句，完整描述（含
"all-or-nothing" 和 "must appear exactly once"）：

```python
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": (
                    "Edit files by replacing exact blocks of text. Every edit is "
                    "checked before any file is written: if one fails, nothing is "
                    "written."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["edits"],
                    "properties": {
                        "edits": {
                            "type": "array",
                            "description": "The edits to apply, in any order.",
                            "items": {
                                "type": "object",
                                "required": ["path", "old_text", "new_text"],
                                "properties": {
                                    "path": {
                                        "type": "string",
                                        "description": (
                                            "Path to the file, relative to the repository "
                                            "root. Example: src/minicodex/model.py"
                                        ),
                                    },
                                    "old_text": {
                                        "type": "string",
                                        "description": (
                                            "The text to replace, copied from the file. It "
                                            "must appear exactly once -- include whole "
                                            "surrounding lines until it does. Do not write "
                                            "line numbers or diff markers."
                                        ),
                                    },
                                    "new_text": {
                                        "type": "string",
                                        "description": "What to put in its place.",
                                    },
```

描述里三句话各对应正文一个教训：

- **"Every edit is checked before any file is written: if one fails, nothing
  is written"**——把 all-or-nothing 写进描述，模型知道失败不产生半成品。
- **"It must appear exactly once"**——`locate` 的"唯一命中"规则写进描述，
  模型被提前告知要包含足够上下文行让它唯一。
- **"Do not write line numbers or diff markers"**——实测里模型会把
  `@@` 行号或 diff 标记贴进 `old_text`，这句话是正文 §4 测量结果的直接
  落点。

## N4 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| `../../etc/passwd` 被接受 | 只检查绝对路径分支 | 先 `(root / candidate).resolve()` 再 `relative_to(root)`，无条件检查 |
| 一个文件的两处编辑互相覆盖 | 每个编辑独立计划后一起写 | 先 `locate` 全部，再 `write_source` 全部（正文 review 第 1 条：真 bug，实测没见过，故意不修） |
| `str.replace` 改了三处相同的 | 用了 `replace` 而不是精确匹配 | `locate` 分级匹配，多于一处是错误 |
| CRLF 文件被改成 LF | 没注意行尾 | `read_source` 用 `newline=""` 读出实际行尾，`write_source` 还原 |
| 写一半失败留下半成品 | 边写边校验 | `apply_edits` 先全部校验（含语法），再统一写入 |
| 模型贴整个文件 | 描述没说不许 | 描述加 "Do not write the whole file" 类约束（实际落地是 "Do not write line numbers or diff markers"） |
| 编辑后语法错误 | 没检查 | `syntax_error`（`ast.parse`），`None` 以外的文件类型跳过 |
| handler 收到非 dict 的 edit | 参数没校验 | `isinstance(item, dict)` + 缺失字段列表一次性报出 |
