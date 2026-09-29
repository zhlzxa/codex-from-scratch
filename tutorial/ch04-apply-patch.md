# 第 4 章 · 改文件

> **代码**：`steps/step04_apply_patch/`
> **分支**：`feat/apply-patch`
> **产出**：Agent 能改代码，而且改错的时候不会留下改了一半的仓库
> **前置**：做完第 3 章。§4 的对比来自两家真实模型；代码部分不需要模型。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **diff / patch**：描述"一个文件怎么改"的文字。git 用的 **unified diff** 格式长这样：

  ```
  @@ -12,3 +12,3 @@
   def clean_body(text):
  -    return text.strip().lower()
  +    return text.strip()
  ```

  `@@ -12,3 +12,3 @@` 表示"从第 12 行开始的 3 行"；以 `-` 开头的行删掉，以 `+` 开头的行加上，
  以空格开头的是没变的上下文。
- **行尾（line ending）**：每行末尾那个看不见的"换行"字符。macOS/Linux 用 `\n`（LF），
  Windows 习惯用 `\r\n`（CRLF）。两者在编辑器里看起来一模一样，但字节不同。
- **原子性（all-or-nothing）**：一次改五个文件，要么五个都改成，要么一个都不改，不能停在中间。
- **路径穿越**：`..` 表示上一级目录。`../../etc/passwd` 这样的路径，可以从仓库里一路"爬"出去。
- **语法树（AST）**：第 1 章用过 `ast.parse` 检查 import。它也能用来检查"这段 Python 代码语法对不对"——
  不对就抛 `SyntaxError`。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `import re` | 正则表达式模块：用一种小语言描述"什么样的文字算匹配" |
| `re.escape(s)` | 把 `s` 里有特殊含义的字符（`(`、`.`、`*` 等）转义，让它按字面匹配 |
| `re.finditer(pattern, text)` | 找出所有匹配，每个匹配有 `.start()` 和 `.end()` 位置 |
| `text.find(sub, start)` | 从 `start` 位置开始找 `sub`，找不到返回 `-1` |
| `open(path, encoding="utf-8", newline="")` | 打开文件，并且**不**自动转换行尾 |
| `enumerate(items, 1)` | 边遍历边编号，编号从 1 开始 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/apply-patch
```

---

## §1 这一章要做出来的东西

前三章的 Agent 能读文件、能跑命令、能被正确调用。**它还不能改任何东西。**

改文件听起来比跑命令简单——`open(path, "w")` 就完了。实际上它是目前破坏力最大的一个工具：
跑错一条命令，输出是错的；改错一个文件，**代码库是错的**，而且可能没人立刻发现。

---

## §2 定需求，猜故障

需求：一个 `apply_patch` 工具，模型说"把哪个文件的哪段改成什么"，它就改。

第 3 章学到"别人的清单是猜测，要测"。这一章同样先列一张清单：

| 编号 | 猜测 | 怎么判断 |
|---|---|---|
| F04-01 | 每次都让模型重写整个文件，太费 token | 算一算 |
| F04-02 | 整个文件重写时，模型悄悄漏掉了代码 | 让模型重写一个文件，逐字比较 |
| F04-03 | 模型数不对行号 | 让它写 unified diff，看 `@@` 里的行号对不对 |
| F04-04 | 前一处修改让后面的行号全部错位 | 同上 |
| F04-05 | 模型算不对 `@@ -12,7 +12,9 @@` 这种头 | 同上 |
| F04-06 | 要替换的文字在文件里出现了不止一次，改错了地方 | 构造一个有重复代码块的文件 |
| F04-07 | 模型抄回来的原文，空白和缩进和文件不一样 | 让模型贴原文，逐字比较 |
| F04-08 | Windows 文件的 CRLF 和模型给的 LF 对不上 | 编辑一个 CRLF 文件 |
| F04-09 | Tab 和空格看起来一样、字节不同 | 编辑一个用 Tab 缩进的文件 |
| F04-10 | 中文注释让位置计算出错 | 编辑一个带中文的文件 |
| F04-11 | 改五个文件，第三个失败，前两个已经改了 | 构造一个第三处对不上的补丁 |
| F04-12 | 编辑落到了仓库外面 | 传一个 `../` 开头的路径 |
| F04-13 | 改完之后文件语法坏了 | 让一次编辑产生语法错误 |

然后同样：**先写一个最直白的版本，再逐条去撞。**

---

## §3 最直白的版本，以及第一个问题

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

能动。现在要求改 `clean_body` 的空值保护，让它返回 `None`：

```
>>> out = apply_patch(p, '    if not text:\n        return ""',
...                      '    if not text:\n        return None')
>>> out.count("return None")
3
```

**三个函数的空值保护全被改了。** 而且没有任何提示——`str.replace()` 默认替换**所有**匹配，
做完了就报告成功。**F04-06 成立。**

这不是边界情况，而是 `str.replace()` 的定义。空值检查、日志行、`return None` 这种常见的代码块，
在一个文件里出现两三次是常态。

> **第一条规则：歧义是错误，永远不要去猜。**
>
> 如果要替换的文字匹配到多个位置，唯一正确的做法是拒绝并说明——**没有任何信息**能告诉我们模型
> 指的是哪一个。挑第一个、挑最后一个、挑最像的，都是在替模型做一个它没做的决定。

---

## §4 那模型该怎么说"改哪里"？先去问它

主流做法有两种，选哪个不能拍脑袋：

- **unified diff**：`@@ -12,7 +12,9 @@` 加上下文行和 `+`/`-`。git 用的就是它。
- **上下文锚定**：贴一段原文，说替换成什么。不要行号。

第一种看起来更"标准"。但它要求模型**做算术**——数出这一段从第几行开始、有几行。模型能不能做对，
只能测。同一个编辑任务、同一个文件、两种格式，两家模型各采样三次。

（测量方法和第 3 章 §3 的 `probe.py` 一样：换掉工具描述和问题，把模型的原始输出打出来。
作者当时用的脚本没有留在仓库里，下面是它的结果。）

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

Ollama 三次里错一次，而且错得很彻底：说是第 20 行，实际在第 17 行，声明的范围还超出了文件末尾。
**如果照着这个行号去切文件，切到的是别的代码。** F04-03、F04-05 成立。

OpenAI 那两次更有意思。它没拒绝，也没算错——**它换了一种格式**：

```
*** Begin Patch
*** Update File: src/textutil.py
@@
 def clean_body(text: str) -> str:
```

这是 codex 自己的补丁格式。注意那个 `@@` **后面是空的**——没有行号，下面直接跟上下文行。

> 这个发现说明两件事：
>
> 1. **这个格式已经进了模型的训练数据。** 要求 unified diff，它三次里两次给的是 codex 的格式。
> 2. **而这个格式恰恰不含行号。** 一个见过大量补丁的模型，自己选的写法里没有算术——
>    这是对"别让模型数行"最有力的一票，而且不是我投的。

### 4.2 要求上下文锚定

同一个任务，工具改成 `old_text` / `new_text` 两个参数，描述里写清楚"必须在文件里恰好出现一次，
不唯一就把周围的行也包进来"：

```
Ollama:  1-3: [EXACT, unique] 'return text.strip().lower()'
OpenAI:  1-3: [EXACT, unique] 'def clean_body(text: str) -> str:'
```

**两家，六次，全部精确、唯一地匹配，一个字符都不差。** 两家选的锚点不同：Ollama 贴的是要改的那一行，
OpenAI 贴的是函数签名那一行。都对——都是文件里唯一的。

### 4.3 有歧义的时候呢

换成 §3 那个任务：改 `clean_body` 的空值保护——那段代码在文件里一模一样出现了三次。

```
Ollama:  1-3: [unique -- widened to include clean_body]
OpenAI:  1-3: [unique -- widened to include clean_body]
```

**两家 3/3 都主动把函数签名那一行包了进来**，让锚点变唯一。描述里这一句起了作用：

```
If the text you want to change is not unique, include surrounding lines until it is.
```

> **这和第 3 章的结论不矛盾，合起来才完整。**
>
> 第 3 章：描述里写"命令超过 30 秒会被杀"，模型 6/6 照样发两分钟的命令——**无效**。
> 这一章：描述里写"锚点不唯一就扩大范围"，模型 6/6 照做——**有效**。
>
> 区别在于要求的是什么。**前者要求模型改变它对外部世界的预期**（它没法让测试跑得更快）；
> **后者只要求模型改变自己输出的样子**——那完全在它控制之内。
>
> 写描述之前先问一句：我要求的这件事，模型自己做得到吗？

### 4.4 顺便测的一件事，以及它为什么不算数

还测了"只给一个重写整个文件的工具，模型会不会漏掉东西"（F04-02）。两家都完整地交回了整个文件，
只改了该改的地方（342 个字符变成 334 个，正好是删掉的 `.lower()` 那 8 个字符）。

**但这个结果不能下结论。** 目标文件只有 20 行——20 行的文件模型可以完整背下来。真正的风险在
几百行的文件上，而那没有测。

如实记一笔：**这一章选择局部编辑而不是整个文件重写，理由是 token 成本（F04-01）和 §4.1 的行号问题，
不是因为测出了"整个文件重写会丢代码"。** 那个没测出来，F04-02 记为"未复现，而且测试太弱，不足以下结论"。

F04-04（前面的修改让后面的行号错位）也就不会发生了：新格式里根本没有行号。

---

## §5 定位：分三级，每一级都要唯一

模型贴回来的 `old_text` 有三种情况，实测都见过（F04-07）：

1. 和文件里逐字相同——最常见
2. 内容对，行尾多了或少了空格——模型抄的时候会"整理"一下
3. 内容对，缩进不同——模型从嵌套的代码里抄出来时会重新缩进

一级一级放宽，**但每一级都必须只找到一个位置**：

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
    """Last resort: the lines are right, the indentation is not.

    Reindenting is a real thing models do when quoting a nested block back,
    and refusing the edit over it wastes a turn. Blank lines inside the block
    are dropped from the pattern because a model rarely reproduces them
    exactly.
    """
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


# -- checking the result ----------------------------------------------------
```

逐个说：

> - **`_exact`**：用 `content.find(old)` 反复查找，收集每一个出现的位置（开始、结束）。
>   注意下一次从 `i + 1` 开始找，这样重叠的匹配也能找到。
> - **`_trailing_insensitive`**：把 `old` 的每一行去掉行尾空白，用 `re.escape` 转义成字面匹配，
>   再在后面接上 `[ \t]*`（"任意多个空格或 Tab"），各行之间用 `\n` 连接。这样文件里行尾多几个空格
>   也能匹配上。
> - **`_indentation_insensitive`**：最后的手段。把 `old` 的每一行两头的空白都去掉、丢掉空行，
>   行与行之间允许任意空白。**缩进错了也能匹配上。**
> - **`_LEVELS`**：三级按顺序排好，每一级附一句说明，用在错误信息里。
> - **`locate`**：按顺序试每一级。恰好一个匹配就用它；多于一个就报错并**列出行号**；一个都没有就试下一级。
>   行号的算法是：匹配开始之前有几个换行符，再加 1。

**错误信息里带行号**，是第 3 章那条实测结论的直接应用：错误信息是给模型的提示。"它出现了两次"
让模型无从下手，"它出现在第 5 行和第 11 行"能让模型知道该往哪边扩大范围。

F04-09（Tab 和空格）由第三级处理：缩进不同也能匹配。

### 5.1 顺序是有意义的，而作者差点没测到

先试精确、再放宽，看起来是显然的。**但"显然"不等于"测过"**——§11 会讲，把这三级的顺序倒过来，
当时全部测试仍然是绿的。

补这个测试的时候还撞出一件事：

```
content = "def a():\n    return 1\n\ndef b():\n        return 1\n"
old     = "    return 1"          # 四个空格

exact hits: [(9, 21), (36, 48)]   # 两个
```

八个空格缩进的那一行，**包含**了四个空格的锚点（前四个空格加上 `return 1`）。所以连精确匹配都报了歧义。

> **纯粹的子串匹配，在嵌套缩进面前自己就有歧义。** 这不是 bug——歧义被正确地报告了——
> 但它说明"精确匹配肯定唯一"这个直觉是错的。（清单外的发现，不修，记录下来。）

---

## §6 F04-08：改一行，整个文件的行尾都变了

一个从 Windows 检出的文件：

```python
>>> path.write_bytes(b'def f():\r\n    return 1\r\n\r\ndef g():\r\n    return 2\r\n')
>>> content = path.read_text(encoding="utf-8")
>>> content
'def f():\n    return 1\n\ndef g():\n    return 2\n'
```

`read_text()` 默认会把 CRLF **在读的时候就转成 LF**。所以模型贴回来的 `\n` 匹配得非常顺利——
这一步没有任何报错。问题出在写回去的时候：

```
before: b'def f():\r\n    return 1\r\n\r\ndef g():\r\n    return 2\r\n'
after:  b'def f():\n    return 99\n\ndef g():\n    return 2\n'

CRLF count before: 5   after: 0
```

**要求改一行，文件里每一个行尾都被改掉了。** 工具的输出看不出任何异常，`git diff` 里整个文件都在变。
**F04-08 成立，而且比"匹配失败"更糟：匹配成功了。**

修法是读的时候不让 Python 转换，自己记住原来用的是什么：

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

> - `newline=""` 让 Python 读写时都**不**转换行尾，原样给你。
> - `read_source` 返回两样东西：统一成 `\n` 的文字（用来匹配，模型看到的也是 `\n`），
>   以及文件原本用的行尾。
> - `write_source` 写回去之前，把 `\n` 换回原来的行尾。

**统一进来，恢复回去。**

> 一个版本上的坑：`Path.read_text()` 的 `newline` 参数是 Python 3.13 才加的。这个项目声明支持 3.10，
> 所以只能用 `open()`。docstring 里写明了这一点——**不写的话，下一个人一定会"顺手统一"成
> `read_text()`**，这个 bug 就回来了。

一个如实说明的限制：**行尾混用的文件，没有"原来的样子"可以恢复。**

```
before: b'a\r\nb\nc\r\n'
after:  b'a\r\nb\r\nc\r\n'      ← 那个单独的 LF 也被改成了 CRLF
```

只要检测到有 CRLF，就整体按 CRLF 恢复，混用的文件会被统一。这个不修——**没有正确答案，只有取舍**，
写进了注释和测试里。

F04-10（中文）：所有位置都是在解码后的 `str` 上算的，按字符计数，不会切坏一个汉字。同样的代码
如果在 `bytes` 上算，就会出问题。测试里专门放了中文内容和中文锚点。

---

## §7 F04-11：五处编辑，第三处失败

一个补丁改五个文件，第三个的锚点对不上：

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

**仓库现在处在一个模型从没要求过的状态里**，而且模型不知道——它只收到一句"第三个失败了"。
它下一步可能重发整个补丁（于是 f1、f2 的锚点也对不上了），也可能以为什么都没发生。**F04-11 成立。**

修法是把"检查"和"写入"分成两段：**全部通过，才动第一个字节。** 失败时磁盘上什么都没变，模型收到
一条错误，重发整个补丁是安全的。§10 会看到完整的代码。

---

## §8 F04-12：第 3 章留下的一个洞

`apply_patch` 要防止编辑落到仓库外面。第 3 章已经写了路径解析，直接复用：

```python
path, error = resolve(edit.path, root)
```

顺手测一下：

```
'../../../../etc/passwd'   ->  OK: /etc/passwd
```

**通过了。** 第 3 章的 `resolve()` 是这么写的：

```python
if candidate.is_absolute():
    try:
        relative = candidate.resolve().relative_to(root)
    except ValueError:
        return None, tool_error("that path is outside the repository", ...)
    candidate = relative
```

越界检查**只在"绝对路径"这个分支里**。`../../../../etc/passwd` 不是绝对路径，所以那段代码根本没运行。

而第 3 章的测试，用的是一个**绝对路径**。测过的分支就是能工作的分支，相对路径爬出去的情况从来没被执行过。
整整一章，测试全绿。

> **你的测试证明的是你测过的东西，不是你以为的东西。**
>
> 这个洞不是"忘了写检查"——检查写了，测试也写了，两者还互相印证。是**检查和测试犯了同一个错误**：
> 都默认了"越界"等于"绝对路径"。

修法是不再区分两种写法。`paths.py` 的 `resolve` 改成：

```python
def resolve(raw: str, root: Path) -> tuple[Path | None, str | None]:
    """(path, None) if it resolves to a file inside `root`, else (None, error).

    The error is always a three-part message -- chapter 3 measured what
    happens with the other kind.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, tool_error(
            'this tool needs a "path" argument, a non-empty string',
            do_this='Example: {"path": "src/minicodex/model.py"}',
        )

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
    if full.is_dir():
        return None, tool_error(
            "that path is a directory, not a file",
            you_sent=raw,
            do_this="Send the path of a file, or use run_shell with ls to list the directory.",
        )
    if not full.exists():
        near = _candidates(Path(raw).name, root)
        if len(near) == 1:
            do_this = f"Send this instead: {near[0]}"
        elif near:
            do_this = "Did you mean one of these? " + ", ".join(near[:_MAX_SUGGESTIONS])
        else:
            do_this = "Use run_shell with ls or find to see what exists, then try again."
        return None, tool_error("no such file in the repository", you_sent=raw, do_this=do_this)

    return full, None
```

**先解析，再判断落在哪。** `(root / candidate).resolve()` 把路径挂到仓库根目录下，再展开所有的 `..`，
得到"它实际指向哪里"。然后无条件地检查它在不在仓库里。绝对路径越界和相对路径爬出去，本来就是
同一个问题问了两遍；合并之后代码还短了——`is_absolute()` 那个分支整个删掉了。

> 错误信息也从 "only reads files" 改成了 "only touches files"：`apply_patch` 会写文件，"读"不准确了。

注意 `src/../a.py` 仍然是合法的：它解析之后还在仓库里。**一刀切禁掉 `..` 会更简单，也会误伤。**

这是一个**已有的 bug**，不是这个功能的一部分，所以单独提交，而且排在功能前面：

```bash
git add src/minicodex/paths.py
git commit -m "fix: a relative path could climb out of the repository"
```

> **先修 bug，再加功能。** 这个修复混在几百行的新功能里，审查的人不会注意到那几行才是最重要的；
> 单独一个提交，还能被单独挑出来用到别的分支上。

---

## §9 F04-13：改完了，文件还能用吗

匹配成功、写入成功，不代表结果是对的：

```python
>>> ast.parse('def f():\nreturn 1\n')
SyntaxError: expected an indented block after function definition on line 1
```

模型抄错缩进、少个括号，文件就坏了。而 Agent 会继续跑，直到某个测试因为 `SyntaxError` 失败——
那时候已经隔了好几轮。**F04-13 成立。**

写入之前先解析一遍：

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

只对 `.py` 文件生效。`.md`、`.json`、`.rs` 照写不误——**我们没有它们的解析器，而"不检查"比
"不让编辑"好得多**。

---

## §10 拼起来

### 10.1 `apply_edits`：先全部检查，再全部写入

`patch.py` 的主函数，第一版是这样的：

```python
def apply_edits(edits: list[Edit], root: Path) -> str:
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
            return tool_error(f"{where}: {why}", you_sent=edit.old_text, do_this=...)

        start, end = span
        updated = content[:start] + edit.new_text + content[end:]

        broken = syntax_error(path, updated)
        if broken is not None:
            return tool_error(f"{where}: the edit would leave the file unparseable: {broken}", ...)

        planned.append((path, updated, ending))

    for path, text, ending in planned:
        write_source(path, text, ending)

    names = ", ".join(sorted({e.path for e in edits}))
    return f"Applied {len(edits)} edit(s) to {names}."
```

> - **`Edit`** 是一个冻结的数据类，装一处编辑：文件路径、要替换的原文、新的内容。
> - 第一个循环只**计划**：解析路径（§8）、读文件（§6）、定位（§5）、拼出改后的内容、检查语法（§9），
>   把结果放进 `planned`。任何一步失败，立刻返回错误——此时还什么都没写。
> - 第二个循环才**写入**（§7）。
> - `where`：只有一处编辑时就是文件名；多处时写成"第 2 处，共 5 处（文件名）"，让模型知道是哪一处出了问题。
>   `enumerate(edits, 1)` 让编号从 1 开始，和人数数的习惯一致。
> - `{e.path for e in edits}` 是一个**集合推导式**，去掉重复的文件名；`sorted` 排好序。

### 10.2 接进工具表

`tools.py` 加一个 `apply_patch` 工具函数和它的描述。这是 `tools.py` 的全部内容：

```python
"""The tools the agent may call, and the schema the model is shown.

The wording in `TOOL_SCHEMAS` is not decoration. Chapter 3 measured what
changes when it changes, and what does not:

  - Saying "relative to the repository root" fixed the path both models got
    wrong -- but only while the example in the description happened to be the
    answer. The rule is enforced in `paths.resolve()` for that reason.
  - Saying "commands are killed after 30 seconds" changed nothing at all: six
    runs out of six across both providers sent a two-minute command anyway.
    The sentence stays because it costs almost nothing and helps a human
    reading the request log, but the thing that actually redirects the model
    is the error message it gets after the kill.
  - Saying "do not use this to read a file" on `run_shell` was measured and
    made no difference here, because `read_file` and `run_shell` already say
    what they are. It is not added. Two tools whose names did NOT say what
    they were (`fetch_content` / `get_text`) did need it -- both providers
    picked the wrong one 3/3 until each description said what it was not for.
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any

from minicodex.patch import Edit, apply_edits
from minicodex.paths import resolve
from minicodex.shell import DEFAULT_TIMEOUT, ShellSession
from minicodex.shell import run_shell as _run_shell
from minicodex.tool_errors import tool_error


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


async def read_file(root: Path, args: dict[str, Any]) -> str:
    """Read a UTF-8 text file from inside the repository.

    `Path.read_text()` blocks, and a blocking call inside `async def` stops the
    whole event loop rather than just this task.  With one tool at a time nobody
    notices; once chapter 8 runs tools concurrently the concurrency quietly
    turns into a queue.  Ruff's ASYNC240 rejects the direct call, which is why
    those rules are enabled.

    `errors="replace"` for the same reason chapter 2 put it on the subprocess
    output: a file with one bad byte should come back slightly wrong, not as
    an exception that discards the whole read.
    """
    path, error = resolve(args.get("path"), root)
    if error is not None:
        return error
    assert path is not None
    return await asyncio.to_thread(_read, path)


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


def default_tools(root: Path | None = None) -> dict[str, Any]:
    """Build a fresh tool table bound to one repository and one shell session.

    Both tools are `functools.partial` now. `ShellSession` holds state (cwd,
    env) that belongs to one conversation; `root` is what makes it possible to
    decide whether an absolute path is inside the repository, and to name a
    near-miss file in the error. Neither belongs to the process, so neither
    lives at module level.
    """
    root = (root or Path.cwd()).resolve()
    session = ShellSession()
    return {
        "read_file": functools.partial(read_file, root),
        "run_shell": functools.partial(_run_shell, session),
        "apply_patch": functools.partial(apply_patch, root),
    }


def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """What the model is shown.

    A function taking the timeout rather than a module-level constant with the
    number typed into the string: chapter 2 shipped `f"...killed after {30}
    seconds"` next to `DEFAULT_TIMEOUT = 30.0`, which agreed only because both
    were written on the same afternoon. Nothing would have caught the day
    someone changed one of them. Now there is only one number, and a test
    asserts the sentence matches it.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a UTF-8 text file from the repository and return its contents."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["path"],
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": (
                                "Path to the file, relative to the repository root. "
                                "Example: src/minicodex/model.py"
                            ),
                        }
                    },
                },
            },
        },
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
                                },
                            },
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
                    f"commands are killed after {timeout:.0f} seconds."
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


# Kept as a module-level name because `__main__` and the tests both want the
# default, and calling it once here is cheaper than threading it through.
TOOL_SCHEMAS: list[dict[str, Any]] = tool_schemas()

__all__ = ["TOOL_SCHEMAS", "default_tools", "read_file", "tool_error", "tool_schemas"]
```

> - **`apply_patch(root, args)`**：先检查模型发来的 JSON 形状——`edits` 必须是列表，每一项必须是字典，
>   `path`、`old_text`、`new_text` 三个都必须是字符串。缺哪几个**一次全部列出来**，少一轮来回。
>   形状对了，就交给 `apply_edits`，用 `asyncio.to_thread` 放到线程里跑（读写文件会卡住事件循环）。
>   **分工：工具函数管"格式"，`patch.py` 管"语义"。**
> - **`default_tools`**：`apply_patch` 和 `read_file` 一样绑定 `root`。
> - **`apply_patch` 的描述**：三句话各对应一个教训——"一处失败就什么都不写"（§7）、"必须恰好出现一次，
>   不唯一就扩大范围"（§4.3 实测有效）、"不要写行号或 diff 标记"（§4.1）。
>   `edits` 是一个**对象数组**：`"type": "array"` 加上 `"items"` 描述每一项的样子。

`__main__.py` 不用改：它调用的 `default_tools()` 已经包含了新工具。

```bash
git add .
git commit -m "feat: edit files by replacing exact blocks, all or nothing"
```

### 10.3 审查时发现：同一个文件的第二处编辑，吞掉了第一处

自己审查这次改动时，冒出一个问题：**如果模型对同一个文件发了两处编辑，会怎样？**

看 10.1 的代码：每一处编辑都是 `read_source(path)` 从**磁盘**上读原文，各自拼出"改后的内容"。
两处都算完才写——第二处写入的内容里，**没有**第一处的修改。结果是最后写的那一处赢了：

```python
>>> apply_edits([Edit("a.py", "A = 1", "A = 10"), Edit("a.py", "B = 2", "B = 20")], root)
'Applied 2 edit(s) to a.py.'
>>> (root / "a.py").read_text()
'A = 1\nB = 20\n'
```

**报告"两处都改好了"，实际上丢了一处。** 这是一个 🟡 静默故障，清单上没有它。

> 作者最初的处理是"实测里没见过模型给同一个文件发两处编辑，先不修，记录下来"，并写了一个测试
> 断言当前的行为——名字却叫 `test_several_edits_to_one_file_all_land`（"都生效了"），断言的内容恰恰相反。
> 改写这本书时把它修掉了：**一个报告成功、实际丢数据的工具，不该因为"还没见过"就留着。**
> "不为没见过的行为写代码"说的是不要为想象中的需求加功能，不是说可以对已知会丢数据的路径视而不见。

修法是：同一个文件的多处编辑**按顺序叠加**，第二处在第一处改完的内容里定位。语法检查也挪到最后，
在一个文件的**所有**编辑都应用完之后才检查——否则两处"合起来才合法"的编辑（比如一处打开括号、
一处关上）会在一半的时候被拒绝。

改完之后 `patch.py` 的全部内容：

```python
"""Applying an edit the model described, to a file it cannot see while it types.

Everything here exists because of something measured in chapter 4.

`str.replace()` is not one of the options: asked to change one of three
identical guard clauses, it changes all three and reports success.

Matching is graded. A model retyping a block gets the characters right and
the whitespace approximately right, so an exact match is tried first, then
one that ignores trailing whitespace, then one that ignores indentation
entirely. Each level must find exactly one site. Finding several is an error
at every level -- guessing which one was meant is how the wrong function
gets edited, silently.

Line endings are normalised on the way in and restored on the way out. A
CRLF file read with universal newlines matches happily and then writes back
as LF, so a one-line edit rewrites every line ending in the file. That is
invisible in the tool output and enormous in the diff.
"""

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


# -- reading and writing without disturbing what we did not touch -----------


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


# -- finding the site -------------------------------------------------------


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
    """Last resort: the lines are right, the indentation is not.

    Reindenting is a real thing models do when quoting a nested block back,
    and refusing the edit over it wastes a turn. Blank lines inside the block
    are dropped from the pattern because a model rarely reproduces them
    exactly.
    """
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


# -- checking the result ----------------------------------------------------


def syntax_error(path: Path, text: str) -> str | None:
    """A parse error, for the file types we can parse. `None` otherwise."""
    if path.suffix != ".py":
        return None
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return f"{exc.msg} at line {exc.lineno}"
    return None


# -- the whole patch, all or nothing ---------------------------------------


def apply_edits(edits: list[Edit], root: Path) -> str:
    """Validate every edit, then write every edit, or write nothing.

    Measured: writing as it goes leaves the repository half-edited when the
    third of five hunks does not match -- a state the model did not ask for
    and cannot see. One failure, nothing written, one error to act on.

    Edits to the same file apply in the order given, each one located in the
    text the previous one produced.
    """
    if not edits:
        return tool_error(
            "the patch contained no edits",
            do_this="Send at least one edit with path, old_text and new_text.",
        )

    # Staged per file: a second edit to the same file is located in the text
    # the first one produced, not in the file on disk.  Planning every edit
    # against the disk version meant the last write won -- the earlier edits
    # to that file vanished while the result still said "Applied 2 edit(s)".
    staged: dict[Path, tuple[str, str, str]] = {}  # path -> (text, ending, where)
    for index, edit in enumerate(edits, 1):
        where = f"edit {index} of {len(edits)} ({edit.path})" if len(edits) > 1 else edit.path

        path, error = resolve(edit.path, root)
        if error is not None:
            return error
        assert path is not None

        if path in staged:
            content, ending, _ = staged[path]
        else:
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
        staged[path] = (content[:start] + edit.new_text + content[end:], ending, where)

    # Parsed once per file, after every edit to it has been applied: two edits
    # that are only valid together must not be refused halfway.
    for path, (text, _, where) in staged.items():
        broken = syntax_error(path, text)
        if broken is not None:
            return tool_error(
                f"{where}: the edit would leave the file unparseable: {broken}",
                do_this=(
                    "Nothing has been written. Re-read the file and send an edit "
                    "that leaves it syntactically valid."
                ),
            )

    for path, (text, ending, _) in staged.items():
        write_source(path, text, ending)

    names = ", ".join(sorted({e.path for e in edits}))
    return f"Applied {len(edits)} edit(s) to {names}."
```

> - **`staged`** 是一个字典：文件路径 → （改到目前为止的内容、原本的行尾、最后一处编辑的说明）。
>   一个文件第一次出现时从磁盘读；再次出现时，接着用字典里已经改过的内容。
> - 定位失败仍然立刻返回错误，什么都不写。
> - 所有编辑都应用完之后，**每个文件检查一次语法**；全部通过才写入。

```bash
git add .
git commit
```

```
fix: apply several edits to one file in order instead of keeping only the last

Each edit was planned against the file on disk, so a second edit to the
same file was built from the original text and overwrote the first when
written -- while the result still said "Applied 2 edit(s)". Edits are now
staged per file, each located in the text the previous one produced, and
the syntax check runs once per file after all of its edits.
```

---

## §11 测试

### 11.1 `test_patch.py`

新建 `tests/test_patch.py`：

```python
"""What editing a file must refuse to get wrong.

Every case here was reproduced against real files before it was fixed; the
comments say what was measured rather than what seemed likely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minicodex.patch import (
    Edit,
    apply_edits,
    locate,
    read_source,
    syntax_error,
    write_source,
)

TARGET = '''"""Text helpers."""


def clean_title(text: str) -> str:
    if not text:
        return ""
    return text.strip().title()


def clean_body(text: str) -> str:
    if not text:
        return ""
    return text.strip().lower()
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "textutil.py").write_text(TARGET, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# ambiguity
# ---------------------------------------------------------------------------


def test_an_anchor_appearing_twice_is_refused(repo: Path) -> None:
    """`str.replace()` changes all three identical guards and reports success.
    That is the behaviour this whole module exists to not have."""
    result = apply_edits(
        [
            Edit(
                "textutil.py",
                '    if not text:\n        return ""',
                "    if not text:\n        return None",
            )
        ],
        repo,
    )

    assert "appears 2 times" in result
    assert (repo / "textutil.py").read_text(encoding="utf-8") == TARGET


def test_the_ambiguity_error_names_the_lines(repo: Path) -> None:
    """ "It appears twice" leaves the model guessing how to widen. The line
    numbers are the difference between a useful retry and a coin flip."""
    result = apply_edits(
        [Edit("textutil.py", '    if not text:\n        return ""', "    x")],
        repo,
    )
    assert "on lines 5, 11" in result


def test_widening_the_anchor_makes_it_unique(repo: Path) -> None:
    result = apply_edits(
        [
            Edit(
                "textutil.py",
                'def clean_body(text: str) -> str:\n    if not text:\n        return ""',
                "def clean_body(text: str) -> str:\n    if not text:\n        return None",
            )
        ],
        repo,
    )

    assert result.startswith("Applied")
    after = (repo / "textutil.py").read_text(encoding="utf-8")
    assert after.count("return None") == 1
    assert 'def clean_title(text: str) -> str:\n    if not text:\n        return ""' in after


# ---------------------------------------------------------------------------
# graded matching
# ---------------------------------------------------------------------------


def test_an_exact_unique_match_is_used(repo: Path) -> None:
    result = apply_edits(
        [Edit("textutil.py", "    return text.strip().lower()", "    return text.strip()")], repo
    )
    assert result.startswith("Applied")
    assert ".lower()" not in (repo / "textutil.py").read_text(encoding="utf-8")


def test_trailing_whitespace_the_model_added_is_tolerated() -> None:
    content = 'def f(t):\n    if not t:\n        return ""\n    return t.lower()\n'
    span, why = locate(content, "    return t.lower() ")
    assert why is None
    assert span is not None


def test_indentation_the_model_got_wrong_is_tolerated() -> None:
    content = "def f(t):\n    return t.lower()\n"
    _span, why = locate(content, "        return t.lower()")
    assert why is None


def test_the_exact_level_wins_before_a_relaxed_one_can_see_ambiguity() -> None:
    """Order is load-bearing, not cosmetic.

    The second site is indented with a tab, so a four-space anchor matches
    exactly once but matches twice once indentation is ignored. Trying the
    relaxed level first turns an edit that should succeed into an ambiguity
    error.

    This test exists because reversing `_LEVELS` was mutation-tested and every
    other test in this file stayed green -- the ordering had never actually
    been covered. Writing it also turned up something worth knowing: with two
    space-indented sites, the deeper one *contains* the shallower anchor as a
    substring, so even the exact level reports ambiguity. Nesting makes plain
    substring matching ambiguous on its own.
    """
    content = "def a():\n    return 1\n\ndef b():\n\treturn 1\n"

    span, why = locate(content, "    return 1")

    assert why is None, "the exact, unique match should have been taken"
    assert span is not None
    start, _end = span
    assert content[:start].count("\n") == 1, "matched the wrong site"


def test_a_relaxed_level_still_refuses_ambiguity() -> None:
    """Relaxing whitespace must not relax uniqueness -- that combination is
    how the wrong site gets edited while every check passes."""
    content = "def a():\n    return 1\n\ndef b():\n    return 1\n"
    span, why = locate(content, "        return 1")
    assert span is None
    assert why is not None and "2 times" in why


def test_text_that_is_simply_absent_says_so() -> None:
    span, why = locate("def f():\n    return 1\n", "    return 99")
    assert span is None
    assert why == "that text is not in the file"


# ---------------------------------------------------------------------------
# line endings
# ---------------------------------------------------------------------------


def test_crlf_survives_an_edit(tmp_path: Path) -> None:
    """Measured: read_text() translates CRLF to LF, the match succeeds, and
    write_text() saves LF -- so a one-line edit rewrites every line ending in
    the file and the diff shows the whole file as changed."""
    path = tmp_path / "crlf.py"
    original = b"def f():\r\n    return 1\r\n\r\ndef g():\r\n    return 2\r\n"
    path.write_bytes(original)

    result = apply_edits([Edit("crlf.py", "    return 1", "    return 99")], tmp_path)

    assert result.startswith("Applied")
    after = path.read_bytes()
    assert after.count(b"\r\n") == original.count(b"\r\n")
    assert b"return 99" in after
    assert b"\n" not in after.replace(b"\r\n", b"")


def test_lf_files_stay_lf(tmp_path: Path) -> None:
    path = tmp_path / "lf.py"
    path.write_bytes(b"def f():\n    return 1\n")

    apply_edits([Edit("lf.py", "    return 1", "    return 99")], tmp_path)

    assert b"\r" not in path.read_bytes()


def test_read_source_reports_the_ending(tmp_path: Path) -> None:
    crlf, lf = tmp_path / "a.py", tmp_path / "b.py"
    crlf.write_bytes(b"a\r\nb\r\n")
    lf.write_bytes(b"a\nb\n")

    assert read_source(crlf) == ("a\nb\n", "\r\n")
    assert read_source(lf) == ("a\nb\n", "\n")


def test_write_source_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    write_source(path, "a\nb\n", "\r\n")
    assert path.read_bytes() == b"a\r\nb\r\n"


# ---------------------------------------------------------------------------
# non-ASCII
# ---------------------------------------------------------------------------


def test_cjk_content_does_not_shift_offsets(tmp_path: Path) -> None:
    """Offsets are in characters because everything here is `str`. The same
    code over `bytes` would slice mid-character on the comment above.

    The `noqa: RUF001` below is deliberate. That rule flags characters that
    look like ASCII but are not -- a real problem when a Cyrillic `а` sneaks
    into an English identifier. Here the fullwidth comma is simply the correct
    punctuation for the language, and the point of the test is that it
    survives the round trip byte for byte.

    The `noqa: RUF002` on this docstring is not a second exception to the same
    rule so much as a demonstration of it: writing the sentence above put a
    real Cyrillic `а` in this file, and ruff caught it. The rule earned its
    keep on the paragraph explaining why it was being suppressed.
    """  # noqa: RUF002
    path = tmp_path / "cjk.py"
    comment = "# 去掉首尾空白，保留内部结构"  # noqa: RUF001
    path.write_text(f"{comment}\ndef f():\n    return 1\n", encoding="utf-8")

    result = apply_edits([Edit("cjk.py", "    return 1", "    return 99")], tmp_path)

    assert result.startswith("Applied")
    after = path.read_text(encoding="utf-8")
    assert comment in after
    assert "return 99" in after


def test_the_anchor_itself_can_be_cjk(tmp_path: Path) -> None:
    path = tmp_path / "cjk.py"
    path.write_text("# 旧注释\ndef f():\n    return 1\n", encoding="utf-8")

    apply_edits([Edit("cjk.py", "# 旧注释", "# 新注释")], tmp_path)

    assert "# 新注释" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# all or nothing
# ---------------------------------------------------------------------------


def test_a_failure_in_the_third_edit_writes_nothing(tmp_path: Path) -> None:
    """Measured: writing as it goes left files 1 and 2 edited and 3, 4, 5
    untouched -- a state the model never asked for and cannot see."""
    for i in range(1, 6):
        (tmp_path / f"f{i}.py").write_text(f"VALUE = {i}\n", encoding="utf-8")

    result = apply_edits(
        [
            Edit("f1.py", "VALUE = 1", "VALUE = 100"),
            Edit("f2.py", "VALUE = 2", "VALUE = 200"),
            Edit("f3.py", "VALUE = XX", "VALUE = 300"),
            Edit("f4.py", "VALUE = 4", "VALUE = 400"),
            Edit("f5.py", "VALUE = 5", "VALUE = 500"),
        ],
        tmp_path,
    )

    assert "edit 3 of 5" in result
    for i in range(1, 6):
        assert (tmp_path / f"f{i}.py").read_text(encoding="utf-8") == f"VALUE = {i}\n"


def test_several_edits_to_one_file_all_land(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("A = 1\nB = 2\n", encoding="utf-8")

    result = apply_edits(
        [Edit("a.py", "A = 1", "A = 10"), Edit("a.py", "B = 2", "B = 20")], tmp_path
    )

    assert result.startswith("Applied")
    # The first version planned each edit against the file on disk, so the
    # last write won: this assertion read "A = 1\nB = 20\n" -- the first edit
    # silently lost, under a test whose name said the opposite.
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "A = 10\nB = 20\n"


def test_edits_that_are_only_valid_together_are_accepted(tmp_path: Path) -> None:
    """The syntax check runs once per file, after all of its edits: checking
    after each one would refuse the first half of a two-part change."""
    (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    result = apply_edits(
        [
            Edit("a.py", "def f():", "def f(\n"),
            Edit("a.py", "def f(\n", "def f(\n    x=1,\n):"),
        ],
        tmp_path,
    )

    assert result.startswith("Applied"), result
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "def f(\n    x=1,\n):\n    return 1\n"


def test_an_empty_patch_is_refused(tmp_path: Path) -> None:
    assert "no edits" in apply_edits([], tmp_path)


# ---------------------------------------------------------------------------
# staying inside the repository
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "escape",
    ["../../../../etc/passwd", "/etc/passwd", "../outside.py", "src/../../outside.py"],
)
def test_an_edit_outside_the_repository_is_refused(tmp_path: Path, escape: str) -> None:
    """The relative spellings went through until chapter 4: chapter 3 branched
    on `is_absolute()` and only checked containment inside that branch, and
    its test used an absolute path, so nothing ever exercised `..`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.py").write_text("SECRET = 1\n", encoding="utf-8")

    result = apply_edits([Edit(escape, "SECRET = 1", "SECRET = 2")], repo)

    assert "outside the repository" in result
    assert (tmp_path / "outside.py").read_text(encoding="utf-8") == "SECRET = 1\n"


def test_a_path_that_stays_inside_via_dotdot_is_allowed(tmp_path: Path) -> None:
    """`src/../a.py` is inside the repository. Rejecting every `..` would be
    simpler and would also reject this."""
    (tmp_path / "src").mkdir()
    (tmp_path / "a.py").write_text("X = 1\n", encoding="utf-8")

    result = apply_edits([Edit("src/../a.py", "X = 1", "X = 2")], tmp_path)

    assert result.startswith("Applied")


# ---------------------------------------------------------------------------
# leaving the file parseable
# ---------------------------------------------------------------------------


def test_an_edit_that_breaks_the_syntax_is_refused(repo: Path) -> None:
    result = apply_edits(
        [Edit("textutil.py", "def clean_body(text: str) -> str:", "def clean_body(text: str) ->")],
        repo,
    )

    assert "unparseable" in result
    assert (repo / "textutil.py").read_text(encoding="utf-8") == TARGET


def test_the_syntax_error_says_where(repo: Path) -> None:
    result = apply_edits(
        [Edit("textutil.py", "    return text.strip().title()", "    return text.strip(")], repo
    )
    assert "line" in result


def test_non_python_files_are_not_parsed(tmp_path: Path) -> None:
    """We have no parser for these, and refusing to edit them would be worse
    than not checking."""
    path = tmp_path / "notes.md"
    path.write_text("# Title\n\ndef f( :\n", encoding="utf-8")

    result = apply_edits([Edit("notes.md", "# Title", "# Heading")], tmp_path)

    assert result.startswith("Applied")


def test_syntax_error_only_looks_at_python(tmp_path: Path) -> None:
    assert syntax_error(tmp_path / "a.md", "def f( :") is None
    assert syntax_error(tmp_path / "a.py", "def f( :") is not None
```

按主题分组：

> - **`TARGET` 和 `repo` fixture**：§3 那个有三个重复空值保护的文件，放进一个临时"仓库"。
> - **歧义**：锚点出现两次被拒绝；错误信息里有行号；把锚点扩大到包含函数签名后就唯一了。
> - **三级匹配**：精确唯一时直接用；行尾多了空格能容忍；缩进错了能容忍；
>   **`test_the_exact_level_wins_before_a_relaxed_one_can_see_ambiguity`**：精确那一级只有一个匹配、
>   宽松那一级有两个——顺序对，就用精确的那个；顺序反了，就会报歧义。§5.1 说的那个坑让这个测试不好写：
>   两处都用空格缩进时，精确匹配自己就有歧义，所以第二处改用 Tab。宽松的一级也仍然要求唯一。
> - **行尾**：CRLF 改完还是 CRLF；LF 文件保持 LF；`read_source` 报告行尾；`write_source` 能原样写回。
> - **中文**：中文内容不影响位置；锚点本身是中文也可以。测试里有一段关于 lint 的故事，见 §11.3。
> - **全有或全无**：第三处失败时什么都不写；**同一个文件的两处编辑都生效**；两处"合起来才合法"的编辑
>   被接受；空补丁被拒绝。
> - **仓库边界**：`@pytest.mark.parametrize` 列出几种爬出仓库的写法，全部被拒绝；`src/../a.py`
>   这样绕一圈还在仓库里的，允许。
> - **语法**：改坏语法的编辑被拒绝，而且文件没变；错误信息里有行号；非 `.py` 文件不检查。

### 11.2 描述快照第一次真正起作用

第 3 章那个"改描述就要同步改快照"的测试，加上 `apply_patch` 的时候立刻失败了。**这正是它的设计意图。**

但它同时暴露了自己的一个缺口：收集描述的代码只走了 `properties` 的第一层，而 `apply_patch` 的参数是
一个**对象数组**——`path`、`old_text`、`new_text` 的描述根本没被收集到，**包括那句承载全部唯一性要求的话**。

`tests/test_schemas.py` 的 F03-10 部分改成：

```python
# ---------------------------------------------------------------------------
# F03-10  a description edit degrades unrelated tasks
# ---------------------------------------------------------------------------

# Every word the model is shown. Chapter 3 measured that changing a single
# example in a description flipped both providers from 3/3 correct to 3/3
# wrong, so a description edit is a behaviour change and should not be
# possible to make by accident. Updating this dict is the confirmation.
#
# This is the cheap version of F03-10. The real one -- run a task set before
# and after and compare -- needs the harness chapter 14 builds.
EXPECTED_DESCRIPTIONS = {
    "read_file": "Read a UTF-8 text file from the repository and return its contents.",
    "read_file.path": (
        "Path to the file, relative to the repository root. Example: src/minicodex/model.py"
    ),
    "apply_patch": (
        "Edit files by replacing exact blocks of text. Every edit is checked before "
        "any file is written: if one fails, nothing is written."
    ),
    "apply_patch.edits": "The edits to apply, in any order.",
    "apply_patch.edits[].path": (
        "Path to the file, relative to the repository root. Example: src/minicodex/model.py"
    ),
    "apply_patch.edits[].old_text": (
        "The text to replace, copied from the file. It must appear exactly once -- "
        "include whole surrounding lines until it does. Do not write line numbers "
        "or diff markers."
    ),
    "apply_patch.edits[].new_text": "What to put in its place.",
    "run_shell": (
        "Run a shell command and return its combined stdout and stderr. The working "
        "directory persists across calls within one session (cd changes it for "
        "subsequent calls). Backgrounded commands (trailing '&') are not supported. "
        "Long-running or silent commands are killed after 30 seconds."
    ),
    "run_shell.command": "The shell command to run.",
}


def collect_descriptions() -> dict[str, str]:
    """Every description the model is shown, including nested ones.

    The first version of this walked only `properties`, one level deep. When
    chapter 4 added `apply_patch`, whose `edits` parameter is an array of
    objects, the descriptions of `path`, `old_text` and `new_text` were not
    collected at all -- including the sentence that carries the whole
    uniqueness requirement. A snapshot that does not reach the text is a
    snapshot of nothing.
    """
    found: dict[str, str] = {}

    def walk(prefix: str, spec: dict) -> None:
        if "description" in spec:
            found[prefix] = spec["description"]
        for param, sub in spec.get("properties", {}).items():
            walk(f"{prefix}.{param}", sub)
        if "items" in spec:
            walk(f"{prefix}[]", spec["items"])

    for tool in TOOL_SCHEMAS:
        fn = tool["function"]
        found[fn["name"]] = fn["description"]
        for param, spec in fn["parameters"]["properties"].items():
            walk(f"{fn['name']}.{param}", spec)
    return found


def test_F03_10_descriptions_are_pinned() -> None:
    assert collect_descriptions() == EXPECTED_DESCRIPTIONS


# ---------------------------------------------------------------------------
# schema shape
# ---------------------------------------------------------------------------


def test_every_parameter_has_a_description() -> None:
    for key, text in collect_descriptions().items():
        assert text.strip(), f"{key} has an empty description"


def test_every_required_parameter_exists_in_properties() -> None:
    for tool in TOOL_SCHEMAS:
        params = tool["function"]["parameters"]
        for name in params.get("required", []):
            assert name in params["properties"], f"{tool['function']['name']}: {name}"
```

> - **`EXPECTED_DESCRIPTIONS`** 加上了 `apply_patch` 的每一句描述，嵌套的用 `edits[].path` 这样的名字。
> - **`collect_descriptions()`** 里的 `walk` 是一个**递归函数**：它记下当前这一层的描述，然后对每个
>   子参数、以及数组的 `items`，再调用自己。不管嵌套多深都能走到。
> - 另外两个结构检查也改成用 `collect_descriptions`。

### 11.3 lint 自己证明了自己

中文测试里的全角逗号触发了 ruff 的 `RUF001`——这条规则专门抓"长得像英文字符、其实不是"的字符，
比如混进英文代码里的西里尔字母。中文注释里用全角逗号是对的，于是加了 `noqa`（让 ruff 忽略这一行）
并写清理由。

然后在写那段理由时，举例说"比如混进英文标识符里的西里尔字母 а"——**真的打了一个西里尔字母**，
被 `RUF002` 当场抓住。这段话留在了测试的 docstring 里。

> **绕过 lint 必须留下理由。** 而这次的理由本身，成了这条规则值得存在的证据。

```bash
git add tests
git commit -m "test: cover the edit paths, including the one mutation testing found"
```

### 11.4 把修复改回去

| 改回去 | 结果 |
|---|---|
| 有歧义时取第一个匹配 | 失败 ×3 |
| 去掉行尾的恢复 | 失败 ×2 |
| 边检查边写入 | 失败 |
| 去掉语法检查 | 失败 ×3 |
| 路径检查改回第 3 章的写法 | 失败 ×3（绝对路径那一个仍然通过——**精确地复现了那个洞**） |
| **把三级匹配的顺序倒过来** | **最初：全部通过** ← |
| 同一文件的编辑改回各自从磁盘读 | 失败 ×2（改写时加的测试） |

（前六行来自写作时的实测。）

"顺序倒过来全部通过"是这次反向验证真正的收获：**顺序从来没有被测过**。§11.1 里那个用 Tab 构造的测试
就是为此补的。顺序倒过来之后，它报出：

```
assert 'that text appears 2 times (matching ignoring indentation), on lines 2, 5' is None
```

> 反向验证的价值不只是"确认测试有效"——**它会告诉你哪些代码从来没被测过。**
> 一个改了之后没有任何测试失败的地方，说明那行代码可以随便改。

### 11.5 全部跑一遍

```
$ uv run pytest
120 passed, 7 skipped in 10.10s
```

（Windows 上实测；跳过的 7 个是第 2 章的 F02-10。）

---

## §12 回顾：十三条猜测

| 编号 | 猜测 | 结果 | 挡住它的东西 |
|---|---|---|---|
| F04-01 | 整个文件重写太费 token | —— 选择局部编辑的理由之一 | 局部编辑 |
| F04-02 | 整个文件重写时悄悄漏代码 | ❌ **没复现，而且测试太弱**（文件只有 20 行） | 记为未还的账 |
| F04-03 | 模型数不对行号 | ✅ 通过 F04-05 证实 🟢 | 锚点不带行号 |
| F04-04 | 前面的修改让后面的行号错位 | —— 不会发生：格式里没有行号 | —— |
| F04-05 | 模型算不对 `@@` 行号 | ✅ **复现**：一家算错，一家换成了不带行号的格式 🟢 | 上下文锚定 |
| F04-06 | 锚点出现多次，改错地方 | ✅ **复现**：`str.replace()` 改了三处还报告成功 🟡 | 歧义即错误，报出行号 |
| F04-07 | 模型抄回来的空白不一样 | ✅ 实测见过 🔵 | 三级匹配，每级都要唯一 |
| F04-08 | CRLF/LF 对不上 | ✅ **复现，而且更糟**：匹配成功，整个文件的行尾被改写 🟡 | 统一进来，恢复回去 |
| F04-09 | Tab 和空格字节不同 | ✅ 🟢 | 第三级匹配处理 |
| F04-10 | 中文让位置出错 | 🛡 按字符计算，不会发生 🟢 | 全部用 `str` |
| F04-11 | 第三处失败，前两处已写入 | ✅ **精确复现** 🟢 | 先全部检查，再全部写入 |
| F04-12 | 编辑落到仓库外面 | ✅ **复现：第 3 章留下的洞**，相对路径能爬出去 🟢 | 先解析，再判断 |
| F04-13 | 改完语法坏了 | ✅ 🔵 | 写入前 `ast.parse` |
| *新* | 精确子串在嵌套缩进下自己就有歧义 | ⚠️ 写测试时发现 🟢 | 不修（歧义被正确报告） |
| *新* | 描述快照够不到嵌套参数的描述 | ⚠️ 加新工具时才发现 🟣 | 递归收集 |
| *新* | 三级匹配的顺序从没被测过 | ⚠️ 反向验证发现 ⚪ | 补一个用 Tab 构造的测试 |
| *新* | 同一文件的两处编辑只留下最后一处，却报告成功 | ⚠️ 自我审查发现 🟡 | 按文件顺序叠加 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 主动边界测试 · 🔵 长时间运行才出现 · 🟠 看日志发现 · 🟣 代码审查 ·
⚫ 用户报告 · ⚪ lint/类型检查

**这一章有两条问题不是来自新代码**：一条是第 3 章留下的安全洞，一条是第 3 章那个快照测试自己的缺口。
两条都是在**复用**旧代码时撞出来的。

> 新功能最好的测试，往往是**拿旧代码去做一件它没做过的事**。

---

## §13 交给 GitHub

```bash
git push -u origin feat/apply-patch
```

开 PR、等 CI、自己审查、合并。这一章的提交：

```
test: cover the edit paths, including the one mutation testing found
fix: apply several edits to one file in order instead of keeping only the last
feat: edit files by replacing exact blocks, all or nothing
fix: a relative path could climb out of the repository
```

### 13.1 审查时提出的问题

**1 · 对同一个文件的两处编辑，行为是什么？**

> §10.3：原来是最后一处赢，而且报告成功。**已修。**

**2 · `_indentation_insensitive` 把空行都丢掉了，会不会匹配到跨过空行的位置？**

> **回答**：会。这是有意的取舍——模型抄多行代码时经常吞掉或多出空行。代价是匹配可能跨过一个
> 本不该跨的空行。**它是最后一级**，前两级都失败才会用到，而且仍然要求唯一。
> 真出问题时会是一个很难查的 bug，这一条记在这里。

**3 · 语法检查只支持 Python，那 `.js`、`.rs` 呢？**

> **回答**：不检查。加解析器意味着加依赖，而且每种语言一个。**目前只有 Python 是我们自己的代码**，
> 别的语言的文件在这个项目里是数据，不是代码。等第二种语言出现时再说。

**4 · `read_source` 用 `open()` 而不是 `Path.read_text()`，写法不统一。**

> **回答**：`Path.read_text()` 的 `newline` 参数 3.13 才有，项目支持 3.10。理由写进了 docstring——
> **不写的话，下一个人一定会"顺手统一"回去**，§6 的 bug 就回来了。

**5 · §4.4 没能证明"整个文件重写会丢代码"，但这一章还是选了局部编辑。**

> **回答**：对，这条意见是对的，§4.4 里如实写了。**局部编辑的理由是 token 成本和行号问题，
> 不是"整个文件重写会丢代码"**——后者没测出来，因为文件只有 20 行。要论证它，得拿几百行的文件重测。
> **记着这笔账。**

---

## §14 本章给 CI 加了什么

**什么都没加。** 新测试由现有的"跑测试"一步自动运行。

---

## §15 三条主线各自留下了什么

### 主线 A · 需求变代码

**歧义是错误，不是猜测。** 匹配到多个位置时，没有任何信息能告诉你模型指的是哪一个。

**别让模型做算术。** 要它给行号，一家算错、一家换成了不带行号的格式；要它贴原文，两家六次全对。
**你要它做的事，得是它擅长的事。**

**描述能约束模型的输出，约束不了模型对世界的预期。** "锚点不唯一就扩大范围" 6/6 有效；
"命令超过 30 秒会被杀" 6/6 无效。区别在于这件事是不是它自己做得到。

### 主线 B · 工程化交付

| 动作 | 本章的规则 |
|---|---|
| 已有的 bug | 单独一个提交，排在新功能前面 |
| 已知会丢数据的路径 | 不因为"还没见过"就留着；"不为想象写代码"不等于"对已知的 bug 视而不见" |
| 测试的名字 | 名字说的，必须是断言的——名叫"都生效"却断言"丢了一个"，比没有测试更误导人 |
| 绕过 lint | 必须留下理由 |
| 反向验证 | 找出"改了也没有测试失败"的代码 |

### 主线 C · 故障

**第一招：你的测试证明的是你测过的东西，不是你以为的东西。** 第 3 章的检查和测试犯了同一个错误。

**第二招：匹配成功不代表结果正确。** CRLF 那个 bug，匹配是成功的，坏在写回去的时候。

**第三招：拿旧代码去做一件它没做过的事。** 两个旧问题都是这样撞出来的。

---

## 如果你只记住三件事

1. **歧义是错误，不是猜测。** 挑一个就是替模型做了一个它没做的决定，而且没人会发现。
2. **别让模型做算术。** 你要它做的事，得是它擅长的事。
3. **报告成功的工具，也可能在丢数据。** str.replace 改了三处、CRLF 被整体改写、同一文件的编辑被吞掉——
   三次都"成功"了。

---

## 动手

```bash
cd steps/step04_apply_patch
uv sync --all-extras
uv run pytest
```

**建议自己做一遍的三件事：**

1. 把 `_LEVELS` 的顺序改成先宽松后精确，跑测试。只有一个测试会失败——找到它，读懂为什么只有它能发现。
   **然后想想你自己的项目里，有多少行代码处在"改了也没人知道"的状态。**
2. 把 §10.3 的修复撤掉（改回每处编辑都从磁盘读），跑测试，看哪两个失败。
3. 让 `apply_patch` 支持新建文件（`old_text` 为空表示创建）。注意 `resolve()` 要求文件必须存在——
   **想清楚是改 `resolve()` 还是在 `apply_edits` 里绕过它，以及这个决定会不会给 §8 的路径检查又撕开一个口子。**

---

## 选读 · codex 是怎么做的

> 基于写作时（2026 年）的 codex 仓库，以后可能会变。不读不影响后面的内容。

**格式几乎一样。** codex 的 `apply_patch` 用 `*** Begin Patch` / `*** Update File:` / `@@` 的自定义文本格式，
其中 `@@` 后面**不跟行号**，靠上下文行定位。§4.1 里 gpt-5.4-nano 自己输出的就是这个格式。

**为什么是自定义文本而不是 JSON。** 多行代码穿过 JSON 字符串要转义，第 3 章测过转义的三种命运。
codex 让模型少转义一层。这一章仍然用 JSON 参数（`old_text` / `new_text`），因为实测两家都没有转义出错；
**这是一个可以被数据推翻的决定，不是原则。**

**容错匹配也是分级的**，`codex-rs/apply-patch/` 里同样是先精确、再放宽空白。**歧义同样是错误**，不猜。

---

**下一章**：[审批与沙箱](ch05-approval.md)——现在 Agent 能改任何文件、跑任何命令了。包括 `rm -rf`。

---

# 附录 · 对照检查

## T1 · 每个文件是在哪一节写的

本章结束时，你的项目内容应该和 `steps/step04_apply_patch/` 一致（测试函数的先后顺序可以不同）。

| 文件 | 在哪写的 |
|---|---|
| `src/minicodex/paths.py` | §8（`resolve` 先解析再判断） |
| `src/minicodex/patch.py` | §5、§6、§9 各部分，§10.1 第一版，§10.3 最终版 |
| `src/minicodex/tools.py` | §10.2 |
| `tests/test_patch.py` | §11.1 |
| `tests/test_schemas.py` | §11.2（F03-10 部分） |

## T2 · 常见报错对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| 一次编辑改了好几处相同的代码 | 用了 `str.replace()` | 用 `locate`，多于一处就报错（§5） |
| CRLF 文件被整个改成 LF | `read_text()` 读的时候就转换了行尾 | `read_source` / `write_source`（§6） |
| `../../etc/passwd` 被接受 | 只在绝对路径分支里检查越界 | 先 `(root / candidate).resolve()` 再检查（§8） |
| 改了五个文件，第三个失败，前两个已经变了 | 边检查边写 | 先全部检查，再全部写入（§7） |
| 同一个文件的两处编辑只生效了一处 | 每处编辑都从磁盘读原文 | 按文件叠加（§10.3） |
| 编辑后语法错误 | 没检查 | 写入前 `syntax_error`（§9） |
| `test_F03_10_descriptions_are_pinned` 失败 | 加了新工具或改了描述 | 同步更新 `EXPECTED_DESCRIPTIONS`，嵌套参数用 `edits[].path` 这样的名字 |
| ruff 报 `RUF001` / `RUF002` | 中文全角标点，或长得像英文的其他字母 | 确认是有意的，加 `noqa` 并写明理由 |
