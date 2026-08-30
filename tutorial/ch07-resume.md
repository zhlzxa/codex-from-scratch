# 第 7 章 · 中断与恢复

> **代码**：`steps/step07_resume/`
> **分支**：`feat/rollout`
> **产出**：进程被杀掉之后，`--resume last` 接着干；Ctrl-C 停下来的时候，
> 停在一个还能继续的地方
> **你需要**：什么都不需要。本章 36 个测试没有一个联网。
> 只有 §11 的那张表是真发给模型测的。

---

## §1 这一章要做出来的东西

第 6 章结束的时候，Agent 能问、能记、能干活、能在上下文快满的时候自己把中间那段
换成摘要。它唯一还不能做的事是：**活过自己这个进程**。

一个真实场景，你这周一定遇到过：

```
$ minicodex ask "把 client.py 的 send() 加上重试，然后跑测试"
[读 client.py]
[改 client.py]
[跑 pytest —— 这一步要 40 秒]
```

第 40 秒的时候你手滑按了 Ctrl-C。或者笔记本合盖了。或者你 `uv sync` 了一下把
venv 换掉了。或者就是它自己崩了。

**然后呢？**

到第 6 章为止，答案是：什么都没有了。历史在内存里，内存跟着进程走了。
你只能重新问一遍，让它重新读一遍文件、重新想一遍、重新改一遍——如果它上次
真的改成功了，这一遍还会在改过的文件上再改一次。

这一章要做的事，一句话：**把会话一边发生一边写到磁盘上，并且保证写下来的东西
从任何一个位置断掉，都还能被读回来接着用。**

听起来是 `json.dump`。这一章有 10 条清单故障，其中 3 条就是"存盘/读盘"。
但真正值钱的不是那 3 条——

> 清单预测 F07-02 是"工具执行到一半崩溃，磁盘上留下有 call 无 output 的记录"。
> 实测：**20 次杀进程，20 次都停在这个位置。**
>
> 它不是一种异常情况。它是**唯一**的情况。

以及一条反向的：

> 清单预测 F07-03 是"写日志时被 kill，最后一行 JSON 不完整"。
> 实测：三种配置、五次尝试，**一次都撕不出来**。

先做能看见的那件事。

---

## §2 先写一坨：跑完再存

最直接的写法。历史在内存里，跑完了写出去：

```python
result = await agent.run(question)
Path("session.json").write_text(json.dumps(result.history.to_wire()))
```

两行。看起来没有任何可以出错的地方，而且它确实能用——只要那个 `await` 会返回。

把它做成一个能被杀掉的小程序（`probe_rollout.py` 的第一段）：

```python
NAIVE = r"""
import json, sys, time
# The obvious first version: keep the history in memory, write it out at the end.
history = [{"role": "user", "content": "add a retry to the client"}]
for turn in range(50):
    history.append({"role": "assistant", "content": f"turn {turn}"})
    time.sleep(0.05)
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(history))
print("saved", flush=True)
"""
```

跑 0.4 秒，然后 kill：

```
=== the history is written when the run finishes; the run is killed
    file exists: False
    bytes on disk: 0
```

0 字节。文件根本没被创建过。

这个结果本身没什么信息量——谁都知道"跑完才存"在没跑完的时候不会存。
**有信息量的是它的对偶**：一个只在成功路径上执行的持久化，
**恰好在唯一需要它的场景里不执行**。跑完了的会话你根本不需要恢复。

> **这条规则在别处也成立**：所有"收尾时清理/上报/落盘"的代码，
> 都要问一句"如果收尾不发生呢"。如果答案是"那就什么都没有"，
> 那这段代码保护的是不需要保护的那一半。

所以顺序反过来：**先写，再做**。

---

## §3 一条一条落盘

每产生一条历史项，就往文件尾巴上追加一行 JSON。

为什么是 JSONL 而不是一个 JSON 数组？因为数组要有右括号。一个 `[...]` 的文件，
在没写完之前是不合法的 JSON——**它的合法性依赖于未来**，
而我们做这件事的全部理由就是未来可能不会到来。

一行一条，那么"写到哪算哪"的结果永远是"前 N 条都在"。

```python
def _write(self, record: dict[str, Any]) -> None:
    with self.path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
```

`flush()` 之后还要 `fsync()`：`flush` 只是把 Python 的缓冲交给操作系统，
操作系统还可以再攒一会儿。我们要的是"进程没了，这一行还在"，
所以必须落到内核之外。第 0 章的 `Recorder` 已经是这么写的，这里是同一个理由。

`newline=""` 是后来加的，理由在 §8。

**存什么？** 这是这一节唯一一个真正的决定。

第 1 章把历史定义成"事实"，而不是某个供应商的 JSON 方言：`UserMessage`、
`AssistantMessage`、`ToolResult`、`SystemNote`。发送的时候才 `to_wire()`
渲染成方言。

那落盘落哪一层？

- 落 wire format（`to_wire()` 的结果）：写起来最省事，一行 `json.dumps`。
  但它是**某一个供应商的方言**。换供应商之后，旧会话读回来就是错的方言，
  而第 1 章整章就是为了不让方言泄漏到上层。
- 落事实：多写一个 `_dump_item` / `_load_item`，大约 40 行。

选事实。理由不是"更干净"，是**这个文件的寿命比这个程序的任何一次运行都长**——
它是唯一一个会被下一个版本、可能是下一个供应商读到的东西。
把方言写进去，等于把今天的供应商写进了一个明年才会被打开的文件里。

```python
def _dump_item(item: HistoryItem) -> dict[str, Any]:
    if isinstance(item, UserMessage):
        return {"type": "user", "text": item.text}
    if isinstance(item, SystemNote):
        return {"type": "system_note", "text": item.text}
    if isinstance(item, ToolResult):
        return {
            "type": "tool_result",
            "call_id": item.call_id,
            "name": item.name,
            "content": item.content,
        }
    if isinstance(item, AssistantMessage):
        return {
            "type": "assistant",
            "text": item.text,
            "tool_calls": [
                {
                    "call_id": c.call_id,
                    "name": c.name,
                    "arguments": c.arguments,
                    "raw_arguments": c.raw_arguments,
                }
                for c in item.tool_calls
            ],
        }
    raise AssertionError(f"unserialisable history item: {item!r}")
```

`raw_arguments` 那一行是后来补的，补它的过程在 §13——那是本章成本第二高的一个 bug，
而它在补上之前**所有测试都是绿的**。

文件的第一行是一条 meta：

```json
{"type": "meta", "session_id": "20260811T005027-40180", "version": 2,
 "created": 1786434627.6, "cwd": "D:\\...\\step07_resume",
 "provider": "ollama", "model": "gemma4:31b-cloud",
 "sandbox_mode": "read-only", "approval_policy": "on-request",
 "forked_from": null, "forked_at": null}
```

会话文件默认写在 `.minicodex/sessions/`。**这个目录已经在 `.gitignore` 里了**——
插曲 A 发现录制文件没被忽略之后，加的是整个 `.minicodex/`，
所以这一章新造的目录一出生就被挡住了。
（它装的是整段对话，包括 Agent 读过的每一个文件的内容。）
**"忽略这个程序自己创建的目录"比"忽略某个具体文件名"多挡住了一章的东西。**

为什么要记 `cwd` / `model` / `sandbox_mode`？因为**历史里一个字都没提这些东西**，
而历史里全是依赖它们的内容："读 `client.py`"这句话只在某个目录下成立。
§12 是这条 meta 唯一的用途。

---

## §4 谁来调 append

现在有了 `writer.append(item)`，问题是谁调它。

第一版是在 agent 里，历史加一条就写一条：

```python
history.add_user(user_message)
self.rollout.append(UserMessage(user_message))
...
history.add_assistant(turn.text, turn.tool_calls)
self.rollout.append(AssistantMessage(turn.text, turn.tool_calls))
...
history.add_tool_result(call.call_id, output)
self.rollout.append(ToolResult(call.call_id, call.name, output))
```

能跑。而且丑得很明显：**同一件事说了两遍，四个地方**（还有 `add_system_note`）。

丑不是理由。理由是第 1 章已经把这个问题回答过一次了：

> 「每个 tool_call 恰好一个 output」这种规则不封进类，就会有 8 处各自维护，
> 迟早漏一处。

这里是同一个形状：「每一条进历史的东西都要落盘」如果靠四个调用点各自记得，
那第五个调用点（下一章的、下下章的）就会忘。而**忘掉的那一条不会报错**——
它会安安静静地在恢复的时候不见。

所以往下沉一层，放进 `History`：

```python
def __init__(self, observer: Callable[[HistoryItem], None] | None = None) -> None:
    self._observer = observer
    self._items: list[HistoryItem] = []

def _append(self, item: HistoryItem) -> None:
    self._items.append(item)
    if self._observer is not None:
        self._observer(item)
```

四个 `add_*` 方法里的 `self._items.append(...)` 全部换成 `self._append(...)`。
agent 那边只剩一行：

```python
history = History(observer=self.rollout.append)
```

**这是本章唯一一次改第 1 章的代码，而且是加一个可选参数。**
默认 `None`，所以前六章的 1250 个测试一行都不用改——它们描述的行为没有变。

> **该不该抽象？** 按第 61 页那条三次法则，这里是"第二次就抽象"，
> 通常是过早的。但它踩中了第一天就该抽象的第三条例外：**不变量需要被强制**。
> 而且它不是一个新抽象——`History` 已经存在，这是给它加一个 hook，
> 不是发明一个 `PersistenceStrategy`。
>
> 判断标准很具体：**如果漏掉一个调用点，是编译错误、测试红、还是什么都不会发生？**
> 这里的答案是"什么都不会发生，直到某天有人恢复会话"。
> 这种情况下，重复就不能忍。

---

## §5 它到底停在哪儿

现在文件里一直有东西了。**问题变成：它停在哪儿？**

这不是一个可以想出来的问题。写一个跟 Agent 形状一样的写入者——
一条 meta，一条 user，然后循环：assistant（发起调用）→ 工具跑 80 毫秒 →
tool_result——在 20 个略微不同的时刻杀掉它：

```python
AGENT_LIKE = r"""
import json, sys, time
path = sys.argv[1]
def w(rec):
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(rec) + "\n"); fh.flush()
w({"type": "meta", "session_id": "probe", "version": 2})
w({"type": "user", "text": "add a retry to the client"})
for turn in range(50):
    w({"type": "assistant", "text": "", "tool_calls": [...]})
    time.sleep(0.08)   # the tool runs
    w({"type": "tool_result", "call_id": f"call_{turn}", ...})
"""
```

```
=== an agent-shaped writer, killed at 20 random-ish moments
    ends on assistant    20/20   UNSENDABLE: call with no result
```

**20/20。**

不是"有时候会停在坏位置"。是**每一次**。

原因一想就明白，但不测就想不到去想：一轮的时间几乎全花在工具上。
写两行 JSON 是微秒级，跑一次 pytest 是秒级。所以随机时刻落在
"call 已写、result 未写"这个区间的概率不是"有一点"，而是**接近 1**。

这句话把这一章的重心整个挪了位置：

> **恢复不是一个边界情况处理，它是主路径。**
> 磁盘上的会话文件，默认状态就是坏的。

而"坏"在这里有精确定义——第 1 章早就给过了：
一个发起了但没有结果的 tool_call，`to_wire()` 拒绝渲染：

```
HistoryError: refusing to send: tool calls with no result: call_1 (slow)
```

（这是真跑出来的。同样的历史发给 OpenAI 是 400，发给 Ollama 是 200 加一段胡话——
第 1 章 F01-02 量过。）

---

## §6 恢复：走第 1 章那扇门

怎么把这个文件读回一个能用的历史？

**第一版**（错的，但你一定会先写这个）：把最后一条扔掉。

```python
items = loaded.items[:-1]
```

拿一个真实的例子试：一轮里模型同时发了两个调用，第一个的结果写下来了，
第二个还没有。磁盘上是：

```
on disk: ['UserMessage', 'AssistantMessage', 'ToolResult']
```

扔掉最后一条（那个 ToolResult），重放：

```
naive 'drop the last item' -> HistoryError: refusing to send:
    tool calls with no result: call_1 (run_shell), call_2 (read_file)
```

**扔掉一条，反而多出一个没答的调用。** 因为那条 assistant 消息里有两个调用，
扔掉结果之后两个都没答了。

**第二版**（还是错的）：往前扔，扔到最后一条是 `ToolResult` 为止。
对上面这个例子，最后一条本来就是 ToolResult，一条都不扔——回到原地。
而原地就是 `call_2` 没人答。

这两版的共同毛病是**在用形状猜合法性**。合法性不是形状，是一个不变量，
而这个不变量第 1 章已经实现好了，就在 `History` 里面。

所以正确的写法不是"找到最后一个完整的轮次"——那是把不变量在第二个地方
重新实现一遍，两份实现迟早会不一致。正确的写法是**重放，并记住每一个
"什么都不欠"的时刻**：

```python
def history(self) -> tuple[History, int]:
    history = History()
    settled: list[HistoryItem] = []
    for item in self.items:
        try:
            _add(history, item)
        except HistoryError:
            break
        if not history.unanswered():
            settled = list(history.items)

    dropped = len(self.items) - len(settled)
    if dropped:
        history = History()
        for item in settled:
            _add(history, item)
    return history, dropped
```

`_add` 就是按类型调 `add_user` / `add_assistant` / `add_tool_result`——
**走第 1 章那扇门，不绕过去**。于是：

- "哪里可以停"这个问题不需要被回答，它是 `history.unanswered()` 的副产品；
- 文件里如果有第 1 章不接受的东西（比如一个 `call_id` 对不上的结果），
  `add_*` 会抛 `HistoryError`，我们就在那里停——已经 settled 的部分仍然是
  一段合法对话；
- 未来第 8 章加并发、第 10 章加子 Agent，只要不变量还在 `History` 里，
  这段代码不用动。

半答的那一轮整轮丢，是这条规则的直接后果，不需要特判：

```python
def test_F07_02_a_partially_answered_turn_is_dropped_whole(tmp_path: Path) -> None:
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("two things", [call("call_1"), call("call_2")])
        history.add_tool_result("call_1", "ok")

    restored, dropped = read_rollout(path).history()
    assert dropped == 2
    assert [type(i).__name__ for i in restored.items] == ["UserMessage"]
```

> **代价要说清楚**：`call_1` 那个工具真的跑过了，它的输出被丢掉了。
> 这是有意的——留着它就要留着发起它的那条 assistant 消息，
> 而那条消息里还有一个没人答的 `call_2`。
> **"丢掉一次真实的工作"和"构造一段没有服务器接受的历史"之间，选前者。**
> §11 就是为了让模型知道这件事发生了。

真跑一次（用第 0 章的 stub 服务器，不花钱）：

```
$ uv run python -m minicodex ask "carry on" --resume 20260811T004000-999
[resumed: 1 incomplete message(s) discarded]
[resumed 3 message(s) from ...\sessions\20260811T004000-999.jsonl]
`src/minicodex/__init__.py` defines the following:
...
[gemma4:31b-cloud | completed after 2 turn(s)]
```

---

## §7 撕不出来的那一行

清单第三条：F07-03，"写日志时被 kill，最后一行 JSON 不完整"。

这条我很确定会复现。所有讲日志的文章都说会。于是写了个 probe 去撞它——
一个进程疯狂往文件里写 JSON 行，0.12 秒之后 kill：

```
=== killed mid-write: small lines (200 bytes), flushed
    lines: 12162   parsed before the first bad one: 12162
    torn tail: False
=== killed mid-write: one 400KB line per write, flushed
    lines: 81   parsed before the first bad one: 81
    torn tail: False
=== killed mid-write: small lines, default buffering, no flush
    lines: 29052   parsed before the first bad one: 29052
    torn tail: False
```

**三种配置，一次都没撕开。**

第三种是特意加的：不 flush，让 Python 自己攒够 8KB 再写——8192 除以 201
不是整数，缓冲区边界必然落在某一行中间，"应该"会撕。没有。

400KB 一行那组也是特意加的（第 6 章 F06-09 那个 400KB 的 stack trace，
写进 rollout 就是一行 400KB），一次写调用要跨很多个页。也没有。

我不打算假装我完全知道为什么（Windows/NTFS 上一次 `WriteFile` 落到内核之后，
进程被 `TerminateProcess` 干掉不影响它写完；Linux 上 `O_APPEND` 的小写入也是
原子的）。**我知道的是：我没能复现它。**

那怎么办？三个选项：

1. **假装复现了**，写"经验表明会撕裂"，配一段防御代码。——不行，
   这本教程整个的立足点就是"贴的每一行输出都是真跑的"。
2. **不写防御**，因为没测到。——也不行。这个防御是三行
   （`try: json.loads(line) / except: break`），而它防的东西如果发生了，
   代价是整个会话读不出来。**成本三行，收益是不可恢复的损失变成可恢复的。**
3. **写防御，并且明确标注它没被测到。**

选 3，而且把这句话写进模块的 docstring 里，不是写在某个 commit message 里：

```python
* **One JSON document per line.**  A reader that stops at the first line it
  cannot parse still has everything before it.  This is cheap insurance rather
  than a measured need: five attempts to tear a line by killing the writer
  mid-write produced zero torn lines (see `probe_rollout.py`), so the guard is
  written and honestly labelled unmeasured.
```

> **一条通用的判断**：防御性代码值不值得写，看两个数——
> **写它的成本**，和**它防的那件事发生时的成本**。
> "我复现不出来"只能降低前者的优先级，不能决定后者。
> 但"我复现不出来"必须被写下来，因为下一个人会以为它是被验证过的。

而这个"停在坏行"的行为本身是要测的——它是我们自己的解析器行为，跟能不能撕开无关：

```python
def test_F07_03_damage_in_the_middle_does_not_resurrect_the_tail(tmp_path: Path) -> None:
    """A bad line stops the read; later lines are not skipped past.

    Skipping would produce a history with a hole in it -- a result whose call
    is missing -- which is the F07-02 shape arriving by a different route.
    """
```

**为什么坏行之后要停，而不是跳过继续读？** 因为跳过会得到一段"中间有洞"的对话——
一个结果的调用不见了。那正是 §6 整节在防的形状，只是从另一扇门进来。
**保留一个前缀（是真实对话），优于过滤出一个看起来完整的东西（是虚构）。**

同一条规则也管"我不认识的记录类型"：

```python
def test_F07_03_an_unknown_record_type_is_also_a_boundary(tmp_path: Path) -> None:
    """Well-formed JSON this version does not understand is not skipped either."""
```

一个新版本写的 `{"type": "reasoning_summary", ...}`，对老版本来说
就是"这里发生过一件我不知道的事"。跳过它，等于假装它没发生过。

---

## §8 两个写者：不是交错，是删除

清单 F07-08："两个进程写同一个 rollout 文件，互相覆盖"。

这条复现了，而且复现出来的东西比清单写的严重一档。

两个进程，各自往同一个文件追加 4000 条记录，一共 8000 条：

```
=== two processes appending to one file, 4000 records each
    records written: 8000
    lines on disk:   5559   from A: 2699   from B: 2832
    unparseable:     28
    records lost:    2469
```

**2469 条记录没了。** 不是错位，不是乱序，是**不存在**。
（这个数每次跑都不一样——再跑一次是 2304——但从来没有低于两千过。）
两个进程各自维护自己的文件偏移量，A 写到 100000 字节处，
B 也从它自己记的 100000 字节处开始写，把 A 刚写的盖掉。

28 条读不出来的行是另一回事，而它的成因很有意思——去看那些坏行的字节，
它们是孤立的 `\r`：

```
b'\r' ... b'\r'
```

Python 在 Windows 上以文本模式写文件，`\n` 会被翻译成 `\r\n`，
**两个字节的写入之间是可以被另一个进程插进去的**。所以撕裂确实存在，
但撕的不是 JSON，是行尾。

这给了一个免费的修法，跟锁无关：

```python
with self.path.open("a", encoding="utf-8", newline="") as fh:
```

`newline=""` 让 Python 别做翻译，只写 `\n`。一个字节的东西撕不开。

真正的修法是**不许有第二个写者**。

```python
def _acquire_lock(self) -> None:
    try:
        fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = self.lock_path.read_text(encoding="utf-8", errors="replace").strip()
        raise RolloutError(
            f"{self.path} is already open by another minicodex (pid {holder or '?'}). "
            f"Resume it there, or delete {self.lock_path} if that process is gone."
        ) from None
    os.write(fd, str(os.getpid()).encode())
    self._lock_fd = fd
```

```
RolloutError: C:\...\s.jsonl is already open by another minicodex (pid 5104).
Resume it there, or delete C:\...\s.jsonl.lock if that process is gone.
```

三个决定，每个都有理由：

- **单独的 `.lock` 文件 + `O_EXCL`**，不是 `fcntl.flock`：后者在 Windows 上不存在，
  而这个项目从插曲 A 开始就在 Windows 上跑测试。
- **锁文件里写 pid**：一个残留的锁是要人来判断的（"那个进程还在吗"），
  而人没法凭空判断。错误信息里给出 pid 和文件路径，**让用户能做决定，
  而不是只能困惑**。第 5 章 F05-07 是同一条：规则要可审计、可撤销、显示来源。
- **不自动清理残留锁**：判断"那个 pid 还活着吗"在跨平台上不可靠
  （pid 会复用）。自动清理猜错一次，就是回到 2469 条记录消失的世界。

### 8.1 而在今天的 CLI 里，这把锁够不着

写完之后我去找触发路径，发现了一件不太舒服的事：

**每次 `minicodex ask` 都会创建一个新文件**（`时间戳-pid.jsonl`），
`--resume` 也是写新文件。所以在今天的命令行下，**两个进程不可能选中同一个文件名**——
除非同一秒、同一个 pid，那不可能。

那这把锁是不是纯仪式？（插曲 B 的 FB-03 就是这个问题：
"为打断循环而引入的接口只有一个实现，纯属噪音"。）

想清楚之后我的答案是：**不是，但理由不是我一开始以为的那个。**

- 现在够不着，是因为**命名方案**恰好保证了唯一性。命名方案是最容易被改的东西——
  下一章加子 Agent，或者哪天有人觉得"resume 应该续写同一个文件"（codex 就是这么做的），
  唯一性立刻消失。**"因为文件名不会撞所以安全"是一个没有写下来的假设。**
- 库这一层够得着：`RolloutWriter(path, meta)` 是公开的，`fork` 会写文件，
  测试也直接用它。
- 成本是 12 行。

但这跟"加一个只有一个实现的接口"是不同的东西：**接口是让别人多一层要理解的东西，
守卫是让一个当前成立的事实变成一个持续成立的事实。** 前者的成本随时间涨，后者不涨。

诚实起见，这句话写进 README 的"deliberately not done"里了：
「the lock is currently unreachable from the CLI」。

还有一件事值得单独说：**那个 8000 条的实验被写成了测试**：

```python
def test_F07_08_two_real_processes_do_corrupt_a_shared_file(tmp_path: Path) -> None:
    """The measurement the lock exists for, at a size that runs in CI.

    Not a test of minicodex: a test of the assumption underneath it.  If
    concurrent appends were safe, the lock would be ceremony.
    """
    ...
    assert len(lines) < 8000, "concurrent appends lost nothing; the lock would be ceremony"
```

它测的不是我们的代码，是**我们代码所依赖的那个前提**。
哪天换个文件系统、换个平台，前提变了，这个测试会红——
那时候它会告诉你"锁可以删了"，这也是有用的信息。

---

## §9 Ctrl-C 打断谁

到这里，"崩溃"这条线走完了。**"用户主动打断"是另一条线，而且它更麻烦**，
因为崩溃是不可控的，打断是可控的——可控就意味着我们要决定它怎么发生。

清单 F07-04 写的是："Ctrl-C 后不知道是杀工具还是杀 turn"。这句话本身就是问题的一半。

在 Python 里，Ctrl-C 在 `asyncio.run()` 下面表现为**任务被取消**，
也就是当前 `await` 的地方抛出 `asyncio.CancelledError`。
而当前 `await` 的地方，按 §5 的测量，**几乎总是在某个工具里面**。

于是第 0 章那个承诺出问题了。第 0 章 F00-05 的修法是：

```python
try:
    return await self.tools[call.name](call.arguments)
except Exception as exc:  # deliberately broad; see the docstring
    return f"Error: {call.name} raised {type(exc).__name__}: {exc}"
```

注释里写着 "deliberately broad"。它不够 broad。

**`asyncio.CancelledError` 从 Python 3.8 起继承 `BaseException`，不是 `Exception`。**

所以 Ctrl-C 会直接穿过这个"永不抛出"的函数。真跑一遍就看得到：

```
the run unwound with CancelledError
HistoryError: refusing to send: tool calls with no result: call_1 (slow)
```

第一行是取消穿过了整个循环。第二行是它留下的东西：
一个发起了、没有结果的调用——**跟崩溃留下的形状一模一样**，
只不过这次它还在内存里，而且是我们自己造成的。

修法：在调用工具的那个 `await` 上单独接一次，**把已经发出去的调用全部答掉**，
然后才让循环结束：

```python
for index, call in enumerate(turn.tool_calls):
    try:
        output = await self._run_tool(call)
    except (asyncio.CancelledError, KeyboardInterrupt):
        for pending in turn.tool_calls[index:]:
            history.add_tool_result(
                pending.call_id,
                "Error: interrupted by the user before this finished. "
                "It may have run partially, or not at all.",
            )
        self.rollout.mark("interrupted", turn=turn_index)
        history.add_system_note(interrupted_note())
        return RunResult(
            final_text, "interrupted", turn_index + 1, history, tuple(compactions)
        )
    history.add_tool_result(call.call_id, output)
```

几个细节，每个都对应一个能写成测试的决定：

**（a）从 `index` 开始，不是只答当前这个。** 一轮里可能有三个调用，
被打断的时候后面两个还没跑。它们也是"发起了没有结果"，
也必须有 output——这是第 1 章的不变量，不是礼貌。

**（b）output 的措辞是 "It may have run partially, or not at all."**
不是 "cancelled"。因为**我们不知道**：工具是 `await` 到一半被取消的，
子进程可能已经写了半个文件。把不确定性交给模型，比替它编一个确定答案好。

**（c）答完就返回，不接着问模型。** 这个决定值得单独说：

```python
async def test_F07_04_the_run_ends_rather_than_continuing(tmp_path: Path) -> None:
    """Cancelling the tool does not mean "skip this tool and carry on".

    The user pressed Ctrl-C to stop something.  A loop that answers the call and
    then asks the model what to do next has spent money to ignore them.
    """
    ...
    assert result.stop_reason == "interrupted"
    assert len(model.sent) == 1
```

"杀工具还是杀轮次"的答案是：**杀工具的动作是为了让轮次能干净地结束**，
而不是为了跳过这个工具继续。用户按 Ctrl-C 是要它停，不是要它换一个工具再来一次。

**（d）不能把这个 `except` 包在整个循环外面。** 那样的话，
在等模型响应的时候按 Ctrl-C，也会被"处理"成一次干净的中断——
而任务实际上没有被取消，`await task` 的人会拿到一个正常返回值。
**对上层撒谎。** 所以这一条也有测试：

```python
async def test_F07_04_an_interrupt_between_turns_is_not_swallowed() -> None:
    ...
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
```

> 这一条是"修在哪一层"的一个小例子（§4.4 那张表）：
> 同一个 `except CancelledError`，包在 `await self._run_tool(call)` 上是对的，
> 包在 `for turn_index in range(...)` 上是错的，
> **而两者都能让"Ctrl-C 不再打印 traceback"这个现象消失。**

---

## §10 被打断的命令还在跑

清单 F07-06："中断时后台进程没清理"，注明"与 F02-08 共用清理机制"。

去看第 2 章的清理机制，它长这样——在超时或者超出输出上限的时候：

```python
if give_up is not None:
    os.killpg(proc.pid, signal.SIGKILL)
```

它在 `if give_up is not None:` 里面。而取消不会让 `give_up` 变成非 None，
取消是从 `await asyncio.wait_for(proc.stdout.read(4096), ...)` 里直接抛出来的。

**所以第 2 章的清理，在第 7 章新开的这扇门上不生效。**
命令继续跑，Agent 进程没了，没有任何东西再持有它的引用——
这就是 F02-08（孤儿进程），**通过一扇它被修好时还不存在的门回来了**。

修法很短：

```python
except asyncio.CancelledError:
    _kill_group(proc)
    raise
```

顺手把 `os.killpg` 抽成 `_kill_group`，因为现在有两个调用点了（三次法则的第二次，
按理该忍——但这里抽出来的同时解决了另一件事：F02-10 那个
"Windows 上没有 killpg"的分支，现在只需要写在一个地方）：

```python
def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if not hasattr(os, "killpg"):  # pragma: no cover - platform branch
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover
        pass
```

测试在 Windows 上会被跳过，而且跳过的时候说出自己的名字：

```python
@pytest.mark.skipif(
    not hasattr(__import__("os"), "killpg"),
    reason="F02-10: killpg is POSIX-only and the Windows equivalent is not built",
)
async def test_F07_06_cancelling_a_command_kills_it() -> None:
    task = asyncio.ensure_future(session.run(f"sleep 5; touch {marker}"))
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.0)
    assert not marker.exists(), "the command outlived the interrupt"
```

测法值得看一眼：它不查进程表，**它查那条命令有没有产生副作用**。
`sleep 5; touch marker` 如果活下来，1 秒之后 marker 还不在，
再等下去它就会出现。断言"东西没被创建"比断言"进程不在了"更接近你真正在乎的事。

---

## §11 告诉模型它被打断了

到这里，机械部分全都做完了：文件在、能读回来、断在哪都能接上、进程也清理了。

**然后是这一章真正的问题**：恢复出来的那段历史，
**它自己不知道自己被截断过**。

具体一点。§6 的规则丢掉了"发起了 apply_patch 但没有结果"的那一轮。
恢复出来的历史长这样：

```
S  你是一个编码 Agent……
U  在 client.py 里给 send() 加重试，然后跑 pytest
A  我先看一下这个文件。            [call_read: read_file(client.py)]
T  (文件内容)
U  接着干
```

看起来完全正常。**而 `client.py` 现在可能已经被改过了**——那个 apply_patch
工具真的执行了，只是结果没来得及写下来。

模型会怎么做？这个问题不能靠想。做成 A/B（`probe_resume.py`，gpt-4o-mini，
分类标准：下一步第一个工具调用是"先去看"还是"直接改"）：

```
=== A no note
    {"assume(patch)": 5}
    verified first: 0/5
```

**0/5。** 五次全部直接调 `apply_patch`。三轮独立跑下来是 **0/13**。

加上一条 system note（`interrupted_note(dropped)` 的第一版，四句话）：

```
=== B interrupted note
    {"verify": 4, "assume(patch)": 1}
    verified first: 4/5
```

看起来修好了。**如果这一章到这里结束，它会是错的。**

### 11.1 安慰剂那一组

一个必须问的问题：起作用的是**这条 note 说的内容**，
还是**"多了一条 system 消息"这件事本身**？

模型对 system 消息是有反应的。一条无关紧要但语气权威的 note，
会不会同样让它变谨慎？加第三组，内容是真的、中性的、不含任何警告：

> "This session was resumed from a file on disk."

```
=== C placebo note
    {"assume(patch)": 5}
    verified first: 0/5
```

**0/5。** 跟什么都不加一模一样。

这一组是整张表的地基：**它证明起作用的是"警告"，不是"有一条 note"。**
没有这一组，B 的 4/5 只能说明"加点什么有用"，
而"加点什么有用"是所有 prompt 玄学的开始。

> **通用做法**：任何"我加了一段话，指标变好了"的结论，
> 都要有一个**内容无关但形式相同**的对照组。
> 否则你测的是"多一段话"，不是"这段话"。

### 11.2 我写的长版本，输给了一句话

既然要拆，就拆干净。第四组：只留一句话，连数字都不要。

> "The previous session ended without finishing its last turn."

```
D one sentence {'verify': 5}
D one sentence {'verify': 5, 'assume(patch)': 1}
```

**10/11。**

而我精心写的那四句话版本，累计跑了 19 次：

| 组 | 内容 | verify 优先 |
|---|---|---|
| A | 什么都不加 | 0/13 |
| C | "已从磁盘恢复"（安慰剂） | 0/5 |
| B | 四句话：数量 + 两句建议 | 10/19 |
| D | 一句话，没有数量 | 10/11 |

**53% vs 91%。** 而且是三轮独立跑，方向一致。

B 是这样写的：

```
The previous session ended without finishing its last turn, and 2 message(s)
were discarded because they were incomplete. Whatever was happening at that
moment did not necessarily complete. Check the state of anything you believe
you changed before continuing.
```

多出来的两句是"建议"。而**建议是模型本来就会的东西**——
"改之前先看看"这件事它知道，不需要我教。
真正它不可能知道的只有第一句：**有一轮消失了**。

那数量要不要留？数量是诊断信息，对人有用。做第五组：一句话 + 数量：

```
E sentence+count {'verify': 6}
```

**6/6。** 再用最终版的字符串（从 `interrupted_note(2)` 真取出来那一份）
又跑 5 次：**5/5**。合计 **11/11**。

于是最终版是：

```python
return (
    "The previous session ended without finishing its last turn. "
    f"{dropped} message(s) were discarded because they were incomplete."
)
```

比第一版短了一半，效果从 53% 到 100%。

> 这条经验和第 3 章 F03-07 看起来矛盾，其实不是。
> 第 3 章的结论是"错误信息要三段式：你传了什么、应该怎么做、例子"，
> 因为那时候模型**不知道该往哪儿改**。
> 这里模型什么都知道，它只是**不知道发生过一件事**。
>
> **信息缺口和行动缺口，要用不同的东西补。**
> 往一个信息缺口里塞行动建议，等于把新闻埋进一段它已经读过的话里。

### 11.3 把措辞钉住

一个被测量过的字符串，是一个**有版本、有数据、不能随手改**的东西。
第 3 章 F03-10 上过一次课：改一个例子，两个供应商从 3/3 掉到 0/3，没有任何人发现。

所以加一个快照测试：

```python
def test_F07_05_the_wording_is_pinned() -> None:
    assert interrupted_note(2) == (
        "The previous session ended without finishing its last turn. "
        "2 message(s) were discarded because they were incomplete."
    ), "measured at 11/11 (probe_resume.py); re-measure before changing it"
```

它拦不住任何人改这句话——它拦的是**顺手**改。
失败信息里带着那个数字和 probe 的名字，改的人会看到自己要打败的是什么。

同样地，那张表被写进了 `interrupted_note` 的 docstring，不是写进 commit message。
**commit message 只有考古的时候有人读；docstring 是改它的人一定会看到的地方。**

---

## §12 环境变了

清单 F07-07："恢复后配置变了（换模型、换 cwd），历史里的环境上下文已过期"。

这就是 §3 那条 meta 存在的理由。比对一下，把不一样的说出来：

```python
def environment_note(meta: SessionMeta, current: SessionMeta) -> str | None:
    fields = [
        ("working directory", meta.cwd, current.cwd),
        ("model", meta.model, current.model),
        ("sandbox mode", meta.sandbox_mode, current.sandbox_mode),
        ("approval policy", meta.approval_policy, current.approval_policy),
    ]
    changed = [(label, was, now) for label, was, now in fields if was and now and was != now]
    if not changed:
        return None
    ...
```

```
This session was resumed and the environment is not the one it was recorded in:
- working directory: was '/repo/x', now '/repo/y'
- model: was 'gemma4:31b-cloud', now 'gpt-4o-mini'
- sandbox mode: was 'read-only', now 'workspace-write'
Re-check anything above that your earlier steps depended on before relying on it.
```

三个细节：

**（a）没变就一个字都不说**（返回 `None`）。每次恢复都来一段的 note，
模型会学会跳过它，而且它每一轮都在花 token——第 6 章 F06-06 讲过，
受保护前缀里的东西是**每一轮都重发**的。

**（b）`if was and now`**：旧文件里没记 `sandbox_mode`，
不等于 sandbox_mode 变了。空值是"不知道"，不是"不同"。这条有测试：

```python
def test_F07_07_unknown_fields_are_not_reported_as_changes() -> None:
    """An old file with no `sandbox_mode` recorded is not a sandbox_mode change."""
```

**（c）只告诉，不阻止。** 历史里全是相对路径，它们在新的 cwd 下面可能指向别的文件。
这个程序没有能力把它们改对——但模型一被告知就明白了。

顺手也量了一下这条 note 的效果（同一个 probe 的第二部分）：

```
=== env A no note              {"assume(patch)": 5}
=== env B environment note     {"verify": 4, "assume(patch)": 1}
```

也是 0/5 → 4/5。

> 这里有个诚实的说明：这个测量**没能区分**"环境 note 传递了具体信息"
> 和"环境 note 让模型整体变谨慎了"。它跟 §11 的 D 组效果类似，
> 而 D 组说的完全是另一件事。
> 换句话说：我知道加了有用，我不知道是不是因为它说的内容。
> §11 的安慰剂组只覆盖了 §11。**这一条记为"效果已测，机制未测"。**

---

## §13 版本与迁移

清单 F07-09："rollout 格式升级后旧文件读不了"。解法写的是"版本字段 + 迁移函数"。

版本字段第一天就有——它是那条 meta 里的 `"version": 1`。
问题是**迁移函数你写不出来，直到你真的需要迁移一次**。

而我在这一章里就需要了一次，起因是重读 `_dump_item`：

```python
"tool_calls": [
    {"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
    for c in item.tool_calls
],
```

`arguments` 是解析后的 dict。第 1 章的 `ToolCall` 还有一个字段叫 `raw_arguments`，
是模型原样发过来的字符串。存 dict 不存字符串，看起来完全等价——JSON 嘛。

不等价：

```
model sent:       {"path":"client.py"}
v1 reconstructs:  {"path": "client.py"}
equal: False
```

冒号后面多了一个空格。

这有什么关系？第 1 章留 `raw_arguments` 的理由就是**不做有损的往返**。
它影响两件真事：

1. **prompt 缓存**。供应商按前缀缓存，历史里的字节变了，缓存就断了。
   第 13 章会详细讲（F13-07），这里只需要知道字节不能随便变。
2. **快照测试**。插曲 A 的 characterization test 是拿请求体逐字节 diff 的。
   会话恢复之后请求体多了几个空格，那个 diff 就不空。

**这个 bug 的可怕之处在于：写完 v1 的时候，所有测试都是绿的。**
`json.loads` 两边都能过，语义完全一样。它只在"有人比对字节"的那天才出现。

于是：加上 `raw_arguments`，版本号从 1 到 2，写迁移：

```python
def _migrate(record: dict[str, Any], version: int) -> dict[str, Any]:
    if version < 2 and record.get("type") == "assistant":
        record = dict(record)
        record["tool_calls"] = [
            # Version 1 had no `raw_arguments`.  Re-encoding the parsed object is
            # the best available reconstruction and is *not* the original string:
            # key order and whitespace are this program's, not the model's.  It
            # is written down here rather than pretended away.
            {**c, "raw_arguments": c.get("raw_arguments") or json.dumps(c.get("arguments") or {})}
            for c in record.get("tool_calls", ())
        ]
    return record
```

注释是这段代码里最重要的部分：**迁移出来的不是原始字节，是一个最佳重建**。
v1 文件里那个信息是真的丢了，永远回不来。迁移能做的是让它可读，不是让它变回原样。
把这句话写在代码里，比写在 CHANGELOG 里有用——下一个人是在这里读到它的。

两条测试，分别锁住"旧的还能读"和"新的不再丢"：

```python
def test_F07_09_a_version_1_file_still_loads(tmp_path: Path) -> None:
    """Version 1 had no `raw_arguments`.  Refusing to open it is the bad fix."""

def test_F07_09_a_version_2_file_keeps_the_bytes_the_model_sent(tmp_path: Path) -> None:
    """The reason the version was bumped, stated as a test.

    A model that sent `{"path":"x.py"}` gets that string back, not this
    program's idea of how to spell it.  Version 1 could not do this, and the
    difference is invisible until something downstream compares bytes.
    """
```

> **"拒绝打开旧文件"为什么是坏修法**：用户的会话没有坏，
> 只是比程序旧。一个升级之后打不开自己昨天的会话的工具，
> 用户学到的教训是"别升级"。第 15 章 F15-04 是同一件事的另一半
> （codex 那一堆 `legacy_*_warning` 模块就是这么来的）。

---

## §14 fork：复制，不是引用

清单 F07-10："从历史中间 fork 出新线，两条线共享了后续写入"。

场景是真实的：跑到第 12 轮发现方向错了，想回到第 6 轮换个思路，
但不想丢掉第 12 轮的那条线。

诱人的实现是**记一个偏移量**：新会话指向同一个文件，加一句"我从第 6 条开始"。
省磁盘，听起来很聪明。

它错在哪儿：那两条线接下来都要往文件尾巴上追加。**这就是 §8 那个"两个写者"，
只不过这次是我们自己安排的。** 而且更糟——它们的记录会真的混在一起，
读的时候 A 线会读到 B 线的轮次，因为它们共享同一个"文件尾巴"。

所以：复制。

```python
def fork(source: Path, *, upto: int | None = None, directory: Path | None = None) -> Path:
    """Start a new session from a prefix of an old one.

    Copies records into a new file.  Not "point the new session at the old file
    and remember an offset": two sessions sharing a file is the two-writer
    configuration again, and the second one's turns would appear in the first
    one's replay.  Copying a conversation costs kilobytes.
    """
```

一段对话几十 KB。**为了省几十 KB 去共享一个可变的尾巴，是这一行里最贵的省。**

测试写成"分叉之后两边各走各的"：

```python
def test_F07_10_a_fork_copies_rather_than_references(tmp_path: Path) -> None:
    ...
    forked = fork(source, upto=2, directory=tmp_path)

    # The parent keeps going after the fork...
    with RolloutWriter(source, meta("parent")) as writer:
        writer.append(UserMessage("parent carries on"))
    child = read_rollout(forked)
    assert len(child.items) == 2

    # ...and the child can be written to without touching the parent.
    with RolloutWriter(forked, child.meta) as writer:
        writer.append(UserMessage("child goes elsewhere"))
    parent_texts = [getattr(i, "text", "") for i in read_rollout(source).items]
    assert "child goes elsewhere" not in parent_texts
```

`--upto N` 可以砍在一轮中间——这不需要 `fork` 操心，
§6 的加载规则会把不完整的尾巴丢掉：

```python
def test_F07_10_a_fork_that_cuts_mid_turn_is_still_loadable(tmp_path: Path) -> None:
    """`--upto 3` can land between a call and its result.

    The same rule that recovers a crashed session covers this: the loader drops
    the unfinished tail rather than the fork having to know about turns.
    """
```

**一个不变量，两个用途。** 这是把规则放在 `History` 里而不是放在"恢复代码"里
的回报——`fork` 完全不需要知道什么叫一轮。

新会话的 meta 记住来路：

```python
meta = replace(
    original.meta,
    session_id=new_session_id(),
    forked_from=original.meta.session_id,
    forked_at=len(kept),
)
```

`minicodex sessions` 会把它显示出来：

```
$ uv run python -m minicodex sessions
  20260811T005035-25944 | gemma4:31b-cloud | read-only | ...\step07_resume  7 msg   [from 20260811T005027-40180]
  20260811T005027-40180 | gemma4:31b-cloud | read-only | ...\step07_resume  5 msg
  20260811T004000-999   | gemma4:31b-cloud | read-only | ...\step07_resume  3 msg   [1 incomplete]

resume with: minicodex ask '<next instruction>' --resume last
```

第三行那个 `[1 incomplete]` 是列表自己算出来的——它对每个会话跑一遍 §6 的加载，
**用同一段代码**。列表如果自己判断"完不完整"，就是那个不变量的第三份实现。

---

## §15 和第 6 章的接缝

第 6 章最后一句话是：

> 而这一章刚刚证明了摘要是有损的，所以落盘的必须是原文，不是摘要。

现在这句话要兑现，而它跟"append-only"是有冲突的。

压缩发生的时候，内存里的历史**被换掉了**：前面 20 轮变成了一份摘要。
一个只能追加的文件表达不了"换掉"。

三个选项：

1. **重写文件**（把新历史整个写一遍）。放弃 append-only，
   于是"最坏情况是缺一个尾巴"这个性质没了——重写到一半被杀，文件是半截。
2. **不写摘要，恢复的时候重放原文**。原文还在，很干净——
   但恢复出来的会话立刻又是那个撑爆窗口的大小，
   **压缩等于没做过**，而且下一轮它会再压一次。
3. **写一条分隔标记，然后把新历史作为新的基线追加在后面。**
   加载的时候，从最后一条标记开始读。

选 3：

```python
self.rollout.mark("compacted", generation=result.generation, replaced=result.plan.drops)
```

```python
if record.get("mark") == "compacted":
    # Everything before this point was replaced in memory by the summary that
    # follows it.  Replaying it would undo chapter 6 on every resume -- the
    # session would come back at the size that made it compact in the first
    # place.
    items.clear()
```

于是第 6 章那句话仍然成立，**只是要说得更准确**：

> **原文留在文件里，只是不参与重放。**

文件是全的——你可以打开它读到第一轮的每一个字，做审计、做 §16 的调试、
第 14 章拿它回放。而**加载器**看到的是最后一次压缩之后的世界。

这是"记录"和"状态"的分离：同一个文件，人读它是记录，程序读它是状态。
`marks` 列表里保留了每一次压缩的代数和替换条数，所以两个视角都能对上。

```python
async def test_compaction_writes_a_new_baseline(tmp_path: Path) -> None:
    """A compacted session resumes at its compacted size.

    The file is append-only, so a replaced history is expressed by a marker and
    a new baseline after it.  Without the marker, resuming replays the turns
    compaction removed and the session comes back at the size that made it
    compact.
    """
```

---

## §16 完整代码：`src/minicodex/rollout.py`

554 行。这里给出所有做了决定的部分；纯搬运的部分（`SessionMeta.to_json`、
`list_sessions`、`resolve`）在 §17 的清点表里说明。

**两处删节**：`new_session_id` 和 `mark` 的 docstring 在下面只留了第一行
（源码里它们各有一段说明，内容就是 §3 和 §15 讲过的那两件事）。
除此之外，下面每一行都和 `src/minicodex/rollout.py` 里的字节一致——
本章末尾的自检就是把所有代码块拼起来逐行 grep 源码，只允许
"故意写的错版本"和这两处删节不匹配。

```python
DEFAULT_DIR = Path(".minicodex") / "sessions"

ROLLOUT_VERSION = 2

_NO_HEADER = "{path}: no session header; not a rollout file"


class RolloutError(RuntimeError):
    """The session file cannot be used as asked."""


@dataclass(frozen=True)
class SessionMeta:
    session_id: str
    version: int = ROLLOUT_VERSION
    created: float = 0.0
    cwd: str = ""
    provider: str = ""
    model: str = ""
    sandbox_mode: str = ""
    approval_policy: str = ""
    forked_from: str | None = None
    forked_at: int | None = None


def new_session_id() -> str:
    """Sortable, unique enough, and readable in `ls`."""
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


class RolloutWriter:
    def __init__(self, path: Path, meta: SessionMeta, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.meta = meta
        self.enabled = enabled
        self._lock_fd: int | None = None
        self._items = 0
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._write({"type": "meta", **meta.to_json()})

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _acquire_lock(self) -> None:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = self.lock_path.read_text(encoding="utf-8", errors="replace").strip()
            raise RolloutError(
                f"{self.path} is already open by another minicodex (pid {holder or '?'}). "
                f"Resume it there, or delete {self.lock_path} if that process is gone."
            ) from None
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
            try:
                self.lock_path.unlink()
            except OSError:
                pass

    def __enter__(self) -> RolloutWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def _write(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def append(self, item: HistoryItem) -> None:
        self._write({"type_version": ROLLOUT_VERSION, **_dump_item(item)})
        self._items += 1

    def extend(self, items: Iterable[HistoryItem]) -> None:
        for item in items:
            self.append(item)

    def mark(self, kind: str, **payload: Any) -> None:
        """Record something that is not a history item -- a turn boundary, an abort."""
        self._write({"type": "mark", "mark": kind, "ts": time.time(), **payload})


NULL_WRITER = RolloutWriter(Path(os.devnull), SessionMeta("null"), enabled=False)


@dataclass
class Rollout:
    meta: SessionMeta
    items: list[HistoryItem] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)
    truncated_at: int | None = None
    path: Path | None = None

    def history(self) -> tuple[History, int]:
        history = History()
        settled: list[HistoryItem] = []
        for item in self.items:
            try:
                _add(history, item)
            except HistoryError:
                break
            if not history.unanswered():
                settled = list(history.items)

        dropped = len(self.items) - len(settled)
        if dropped:
            history = History()
            for item in settled:
                _add(history, item)
        return history, dropped


def read_rollout(path: Path) -> Rollout:
    path = Path(path)
    if not path.exists():
        raise RolloutError(f"no session file at {path}")

    meta: SessionMeta | None = None
    items: list[HistoryItem] = []
    marks: list[dict[str, Any]] = []
    truncated_at: int | None = None

    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                truncated_at = lineno
                break
            if not isinstance(record, dict):
                truncated_at = lineno
                break
            if record.get("type") == "meta":
                meta = SessionMeta.from_json(record)
                continue
            if meta is None:
                raise RolloutError(_NO_HEADER.format(path=path))
            if record.get("type") == "mark":
                marks.append(record)
                if record.get("mark") == "compacted":
                    items.clear()
                continue
            try:
                items.append(_load_item(_migrate(record, meta.version)))
            except (RolloutError, KeyError):
                truncated_at = lineno
                break

    if meta is None:
        raise RolloutError(_NO_HEADER.format(path=path))
    return Rollout(meta=meta, items=items, marks=marks, truncated_at=truncated_at, path=path)


def fork(source: Path, *, upto: int | None = None, directory: Path | None = None) -> Path:
    original = read_rollout(source)
    directory = Path(directory) if directory is not None else Path(source).parent
    kept = original.items if upto is None else original.items[:upto]

    meta = replace(
        original.meta,
        session_id=new_session_id(),
        version=ROLLOUT_VERSION,
        created=time.time(),
        forked_from=original.meta.session_id,
        forked_at=len(kept),
    )
    target = directory / f"{meta.session_id}.jsonl"
    with RolloutWriter(target, meta) as writer:
        writer.extend(kept)
    return target
```

`_NO_HEADER` 那个常量是一个小事故的产物。第一版这两处写了两句不同的话——
循环里是 `"first record is not a session header"`，循环外是
`"no session header; not a rollout file"`。同一个状况，两句话，
测试里 `pytest.raises(match=...)` 立刻撞上：

```
E       AssertionError: Regex pattern did not match.
E         Expected regex: 'no session header'
E         Actual message: '...: first record is not a session header'
```

**一个状况一句话。** 两句话意味着以后要 grep 两次、改两处、
而且总有一处会跟另一处说得不一样。

### agent 那边的接线

```python
def _attach(self, history: History) -> History:
    """Re-point a history at the rollout, writing it out as it goes.

    `compact()` builds a plain `History` -- it has no business knowing about
    files -- so the agent hands the new one the observer and replays it.
    """
    rebuilt = History(observer=self.rollout.append)
    for item in history.items:
        replay(rebuilt, item)
    return rebuilt


async def run(self, user_message: str) -> RunResult:
    if self.resume_from is not None:
        history = self._attach(self.resume_from)
    else:
        history = History(observer=self.rollout.append)
        if self.instructions is not None:
            history.add_system_note(self.instructions)
    history.add_user(user_message)
```

注意 `resume_from` 的时候**不加 instructions**——它已经在恢复出来的历史里了
（第 6 章的受保护前缀就是它）。加第二遍就是在系统消息位置放两份权限声明，
而它们可能还不一样（`--sandbox-mode` 换了的话）。

### CLI

```python
resume_from = None
if resume is not None:
    path = resolve(resume, session_dir)
    loaded = read_rollout(path)
    resume_from, dropped = loaded.history()
    if loaded.truncated_at is not None:
        print(f"[session file damaged from line {loaded.truncated_at}; using what precedes it]")
    if dropped:
        resume_from.add_system_note(interrupted_note(dropped))
        print(f"[resumed: {dropped} incomplete message(s) discarded]")
    note = environment_note(loaded.meta, current)
    if note is not None:
        resume_from.add_system_note(note)
        print("[environment changed since this session was recorded]")
    current = replace(current, forked_from=loaded.meta.session_id)
```

**每一件对模型说的事，也对用户说一遍。** 三行 `print` 不是日志，
是让用户知道"我恢复出来的是一段被截断过的会话"——否则他们看到的是
一个凭空少了两条消息的对话，而没有任何解释。

---

## §17 文件清点

| 文件 | 行数 | 新增/修改 | 完整代码在 |
|---|---|---|---|
| `src/minicodex/rollout.py` | 554 | 新增 | §3、§6、§8、§11、§12、§13、§14、§16 |
| `src/minicodex/agent.py` | 370 | 改 5 处 | §9、§15、§16 |
| `src/minicodex/history.py` | 189 | +1 参数 +1 方法 | §4 |
| `src/minicodex/shell.py` | 274 | 改 2 处 | §10 |
| `src/minicodex/__main__.py` | 319 | 改 6 处 | §16 |
| `tests/test_faults_ch07.py` | 778 | 新增，36 个测试 | 全章分散 |
| `probe_rollout.py` | 166 | 新增 | §2、§5、§7、§8 |
| `probe_resume.py` | 194 | 新增 | §11、§12 |

**没进正文的**：`SessionMeta.to_json` / `from_json`（字段逐个搬运，
每个字段的用途在 §3 和 §12 解释过）；`list_sessions` / `resolve` / `rollout_path`
（路径拼接和排序，`sessions` 命令的输出在 §14 展示过）；
`_load_item`（`_dump_item` 的镜像）；`_add` / `replay`
（六行 `isinstance` 分支，语义在 §6 讲透了）。

自检方式：把本章所有 python 代码块拼起来，逐行 grep 源码。

```
$ uv run pytest
1285 passed, 9 skipped in 16.91s

$ uv run pytest tests/test_faults_ch07.py
35 passed, 1 skipped in 1.25s
```

跳过的那一个是 §10 的 `killpg` 测试，在 Windows 上跳过并说出理由（F02-10）。

---

## §18 收工：commit 与 review

### commit 序列

五个 commit，每个自己能过测试。

```
feat(history): let something watch items being appended

Chapter 7 needs every accepted item on disk. Doing that at the four add_*
call sites in agent.py works and is wrong for the same reason the call/result
invariant is not enforced there: a fifth call site added later forgets, and
forgetting produces no error -- the item is simply missing when somebody
resumes, months later.

Optional and defaulting to None, so all 1250 existing tests still describe the
behaviour they were written for.
```

```
feat(rollout): write the session as it happens, one JSON document per line

Measured (probe_rollout.py): a writer that saves on completion leaves 0 bytes
when killed, which is the only case that needed saving.

Facts, not wire format: this file outlives every run of this program and may
be read by a different provider's build of it. An array would also need a
closing bracket, i.e. its validity would depend on the future.

The line-by-line reader is insurance, not a measured need: five attempts to
tear a line by killing the writer produced zero torn lines. Labelled as such
in the module docstring.
```

```
feat(rollout): recover by replaying through History, not by trimming

An agent-shaped writer killed at 20 different moments ends on an unanswered
call 20/20 -- the wall clock of a turn is spent inside the tool, so that is
where the kill lands. Recovery is the normal path, not an edge case.

"Drop the last item" is wrong and makes it worse: on a turn with two calls
and one result it leaves both calls unanswered. So the loader replays through
add_*/HistoryError and keeps the last point at which nothing was outstanding.
One invariant, one implementation; fork --upto gets the same rule for free.
```

```
fix(agent,shell): answer every issued call when a turn is cancelled

except Exception does not catch CancelledError -- it is a BaseException since
3.8 -- so Ctrl-C escapes _run_tool, whose entire job is to turn failures into
outputs, and leaves the history unsendable:

    HistoryError: refusing to send: tool calls with no result: call_1 (slow)

The clause goes on the tool await, not around the loop: around the loop, a
Ctrl-C while waiting on the model would be reported as a clean interrupted run
without the task actually being cancelled.

Chapter 2 kills the process group on timeout, which was the only way out of
run() at the time. Cancellation is a second way out, and it leaked the
subprocess -- F02-08 through a door that did not exist when F02-08 was fixed.
```

```
feat(rollout): tell the model the turn it cannot see was dropped

Measured against gpt-4o-mini, "did it check before editing" (probe_resume.py):

    nothing                                      0/13
    "resumed from a file on disk" (placebo)       0/5
    four sentences: count + two instructions     10/19
    one sentence, no count                       10/11
    one sentence + count            <- shipped   11/11

The placebo arm is why the rest means anything: the warning is the mechanism,
not the presence of a system note. The careful four-sentence version lost to
the plain one twice on independent runs -- the two sentences of advice are
what the model already knows, and they bury the one thing it cannot know.

Pinned by a snapshot test, F03-10's lesson.
```

### PR 描述

```markdown
## What
The session survives the process. `--resume`, `sessions`, `fork`, and a Ctrl-C
that stops the work without leaving the conversation unsendable.

## Why
Everything before this chapter is lost when the process is. And the loss is not
symmetrical: the wall clock of a turn is inside the tool, so a kill almost
always lands between a call and its result -- 20/20 in measurement. What is on
disk after a crash is, by default, a conversation no provider accepts.

## How
- `rollout.py`: append-only JSONL, facts not wire format, one writer (lock),
  version + migration, fork by copy.
- Recovery replays through `History.add_*` and keeps the last settled point, so
  chapter 1's invariant is the only definition of "a legal place to stop".
- `History` gains an observer hook; the agent no longer has four places to
  remember to write to disk.
- Cancellation answers every issued call, kills the process group, and ends the
  run rather than continuing.

## Testing
35 new tests, none of them networked. Two measurements that are in the tutorial
rather than the suite: `probe_rollout.py` (kills real processes) and
`probe_resume.py` (needs an API key).

    1285 passed, 9 skipped in 16.91s

## Notes for the reviewer
- F07-03 (torn line) **could not be reproduced** in three configurations. The
  guard ships anyway; see the module docstring for why and for the fact that it
  is unmeasured.
- The lock is currently unreachable from the CLI (every run gets its own
  filename). Kept deliberately -- see REVIEW.md #3.
```

### Code review

我扮演 reviewer，五条真实意见。

**#1（正确性）`Rollout.history()` 在 `HistoryError` 之后 `break`，
但 `settled` 可能是空的——这时候返回一个空 `History`，`dropped` 等于全部条数。
调用方会不会以为"恢复成功了，只是没内容"？**

> 接受，但改的是 CLI 不是这里。`dropped` 就是给调用方判断的，
> CLI 已经打印了 `[resumed: N incomplete message(s) discarded]`。
> 空历史 + `dropped == len(items)` 是一个合法结果——它的含义是
> "这个文件里没有任何一段完整的对话"，而这在第一轮就崩溃的会话里是真的。
> 加了一条测试锁住这个组合。

**#2（可测试性）`test_F07_08_two_real_processes_do_corrupt_a_shared_file`
启动两个真进程，写 8000 条记录。这在 CI 上要跑多久？而且它测的不是我们的代码。**

> 本机约 1 秒（整个 ch07 文件 36 个测试 1.25 秒）。保留，理由写在 docstring 里：
> 它测的是**我们代码所依赖的前提**。如果哪天这个断言变绿失败
> （并发追加不再丢数据了），那说明锁可以删——那也是有价值的信息。
> 一个会告诉你"防御可以拆了"的测试，值 1.4 秒。

**#3（设计）锁在 CLI 下面根本触发不了，因为文件名带时间戳和 pid。
这不就是 FB-03 说的"只有一个实现的接口"吗？**

> 不是同一类东西，理由写在 §8.1：接口是**给读代码的人加一层要理解的东西**，
> 成本随时间涨；守卫是**把一个当前偶然成立的事实变成持续成立的事实**，
> 成本不涨。而且这个"偶然"是命名方案，是最容易被改的东西——
> codex 自己就是 resume 续写同一个文件的。
> 已在 README 的 "deliberately not done" 里写明它现在够不着。

**#4（措辞）`interrupted_note` 里那个 `dropped is None` 分支没有被测量过，
但它跟被测量过的那句话长得一样，读代码的人会以为两句都有数据。**

> 接受。加了注释：`# Not measured -- the probe reconstructs the from-disk case.`
> 这条比听起来重要：**一段被测量过的文字旁边的、没被测量过的文字，
> 会免费继承前者的可信度。**

**#5（一致性）`RolloutWriter.append` 写的记录里有 `type_version`，
而 meta 里的字段叫 `version`。两个名字。**

> 接受观察，不改。它们是两个东西：meta 的 `version` 是**文件格式版本**，
> 每条记录的 `type_version` 是**写这条记录时的版本**，
> 在一个被多个版本追加过的文件里（v1 写了一半，升级，v2 接着写）两者会不同。
> 但 reviewer 会问，说明名字不够自明——在 `_migrate` 的 docstring 里
> 补了一句说明。

### CI

这一章不加新的 CI 步骤。

理由：本章 36 个测试全部是普通单元测试，`pytest` 已经在跑。
`probe_rollout.py` 会杀真进程、写几十 MB，`probe_resume.py` 要 API key——
它们是**测量工具，不是回归测试**，进 CI 只会让 CI 变慢变脆。

第 6 章加过一步（属性测试跑 2000 个 case），那是因为那条 bug
在默认的 200 个 case 下抓不到。这一章没有这种情况。

> **不加也是一个决定，也要说理由。** 第 -1 章 F-1-05 讲过：
> 一开始配全套 CI 的人，最后都会把 CI 关掉。
> 每一步 CI 都要能回答"它防的是哪一次真实的回退"。

---

## §19 codex 是怎么做的

对照真实的 codex 源码（不需要你去翻，但如果你翻，能对上）：

- **rollout 也是 JSONL，也是 append-only**，路径按日期分层：
  `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl`。
  日期分层是本章没做的，理由只是规模——一个目录一万个文件的时候你会想要它。
- **文件头也是一条 meta 记录**（`SessionMeta`），也带 `cwd`、`instructions`、
  `originator`、`cli_version`。这一章的 meta 少了后两个。
- **恢复的入口叫 `resume`，也支持"最近一个"**（`codex resume --last`）。
- **`compact_remote.rs` / `compact_remote_v2.rs` / `compact_remote_v2_attempt.rs`
  三个文件并存**——上下文压缩和 rollout 的交互被推倒重来过不止一次。
  §15 那个"标记 + 新基线"的做法是这本教程的选择，不是抄来的；
  三个文件的存在说明这个接缝在真实项目里也不好接。
- **`core/tests/suite/compact_resume_fork.rs`**——文件名就是本章的三个主题
  连在一起。测试文件名是最诚实的故障档案：这三件事会互相干扰，
  他们撞过，所以有一个专门测它们组合的文件。
- **中断语义**：codex 区分 `Op::Interrupt`（打断当前 turn）和更粗的关闭，
  并且有专门的 `abort_tasks.rs` 测试套件。§9 那个"答完所有调用再结束"
  在它那里对应 `TurnAbortReason` 和给模型的 abort 上下文注入。

---

## 如果你只记住三件事

1. **崩溃落在哪里，是可以测的，而且答案不是"随机"。**
   一轮的时间几乎全在工具里，所以杀进程 20/20 都停在"发起了调用、没有结果"
   这个位置。**恢复不是边界处理，是主路径。**
   下次你写"如果文件损坏了……"的时候，先去测一下它损坏成什么样子——
   我以为会撕裂的那条（F07-03）一次都没复现，
   而清单只写了一句话的那条（F07-02）是 100%。

2. **恢复逻辑不要重新定义"合法"。**
   "找到最后一个完整的轮次"听起来是恢复代码的职责，
   但它是第 1 章那个不变量的第二份实现，而两份实现迟早会不一致。
   正确的做法是**重放，让原来那扇门去拒绝**，
   然后记住"什么都不欠"的最后一个时刻。
   回报是 `fork --upto` 砍在半轮中间的时候，一行新代码都不用写。

3. **一段话有没有用，要有安慰剂组；而"我写得更全"经常是更差。**
   0/13 → 加一条中性 note 还是 0/5 → 加一句警告 10/11。
   安慰剂那一组是唯一能证明"起作用的是内容不是形式"的东西。
   而我精心写的四句话版本（带数量、带两句建议）只有 10/19——
   **建议是模型本来就会的，把新闻埋在它已经知道的话里，新闻就没人读了。**

---

## 动手练习

1. 把 `Rollout.history()` 换成"扔掉最后一条"，跑测试。
   看清楚哪几个红、哪几个还是绿的。
   然后手写一个"扔到最后一条是 ToolResult 为止"的版本——
   它会让红的那几个变绿，**但 `test_F07_02_a_partially_answered_turn_is_dropped_whole`
   还是红**。想明白为什么这两个"聪明"的规则都比"重放"差。

2. 把 `History.__init__` 的 `observer` 删掉，改回在 agent 里四个地方各写一行。
   然后给 agent 加一个新功能：在压缩之后写一条 `SystemNote`（第 6 章的摘要头）。
   **看你会不会忘掉那一行。** 忘掉之后，跑一下 `--resume`，
   看恢复出来的历史少了什么、有没有任何东西报错。

3. 跑 `uv run python probe_rollout.py`，在你自己的机器和操作系统上。
   特别看第二段（撕裂）——如果你在 Linux/macOS 上撕出来了，
   **那你就得到了本教程没有的数据**。把它记下来，
   这正是 §7 那三个选项里"标注未测量"的意义：它是可以被后来的人补上的。

4. 给 `interrupted_note` 写第六个变体，比如"上一轮被打断了，
   而且它当时正在执行 `apply_patch`"——把**工具名**也告诉模型
   （提示：`Rollout` 里有那条被丢掉的 assistant 消息，虽然 `history()` 不返回它）。
   用 `probe_resume.py` 的方法量它。如果它比 11/11 更好，
   那就说明"具体是哪个工具"是有信息量的；如果一样，
   那就说明模型只需要知道"有事发生"。**两个结果都值得知道。**

5. 把 §15 那条 `compacted` 标记删掉，然后：跑一个会触发压缩的会话
   （`--context-window 3000`），resume 它，看 `[resumed N message(s)]` 那个数字。
   **不会有任何报错。** 只是会话回到了压缩之前的大小，然后立刻又压一次。
   这是本章最容易漏掉的一条，因为它的症状是"变慢变贵"，不是"坏了"。

下一章：Ch08 · 工具并发——一轮里三个调用，能不能同时跑。
而这一章刚刚把"每个调用必须有输出"从一条规则变成了一条**在异常路径上也要成立**的规则，
下一章会让它同时面对三个异常路径。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 6 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`dataclasses`、`asyncio`、`Path` 基础），这里只讲这一章
正文 §17 明确说"没进正文"的部分。代码摘自
`steps/step07_resume/src/minicodex/rollout.py`，逐段核对过。

先把范围说死：

1. 本附录只解释第 7 章在 `steps/step07_resume/` 里新增的代码。正文 §17
   的清点表说得很清楚：`_dump_item`/`history`/`RolloutWriter`/`append`/
   `mark`/`_acquire_lock`/`read_rollout`/`_migrate`/`environment_note`/
   `interrupted_note`/`new_session_id` 都在正文各节给了完整代码，**不再
   重复**。这里补 §17 明说"没进正文"的：`SessionMeta.to_json`/`from_json`、
   `list_sessions`/`resolve`/`rollout_path`、`_load_item`、`_add`/`replay`。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。

## P1 · `SessionMeta.to_json` / `from_json`：字段逐个搬运

正文 §17 自述"字段逐个搬运，每个字段的用途在 §3 和 §12 解释过"。完整
实现：

```python
@dataclass(frozen=True)
class SessionMeta:
    session_id: str
    version: int = ROLLOUT_VERSION
    created: float = 0.0
    cwd: str = ""
    provider: str = ""
    model: str = ""
    sandbox_mode: str = ""
    approval_policy: str = ""
    forked_from: str | None = None
    forked_at: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "version": self.version,
            "created": self.created,
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "sandbox_mode": self.sandbox_mode,
            "approval_policy": self.approval_policy,
            "forked_from": self.forked_from,
            "forked_at": self.forked_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SessionMeta:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})

    def describe(self) -> str:
        where = self.cwd or "?"
        return f"{self.session_id} | {self.model or '?'} | {self.sandbox_mode or '?'} | {where}"
```

三个要点：

1. **`to_json` 是逐字段手写**而不是 `dataclasses.asdict()`——显式列出每个
   字段，新增字段时这里会漏（漏了就是"写盘时悄悄丢字段"，测试能抓到）。
   这是全书"格式变换显式化"的一贯选择。
2. **`from_json` 用 `cls.__dataclass_fields__` 过滤未知键**——旧版本文件的
   字段子集、未来版本的超集都能读：**丢弃不认识的键、缺失的键取默认值**，
   这正是正文 §2.3 里"会话文件 11/11 全部可读"的技术（加字段不算破坏性
   变更）。`**{k: v for ... if k in known}` 只把认识的键传给构造器。
3. **`describe` 是给人看的**（`sessions` 命令的输出）——session_id、
   model、sandbox_mode、cwd 拼一行，`or '?'` 处理空字段。

## P2 · `_load_item`：`_dump_item` 的镜像

正文 §17 自述"`_load_item`（`_dump_item` 的镜像）"。完整实现：

```python
def _load_item(record: dict[str, Any]) -> HistoryItem:
    kind = record.get("type")
    if kind == "user":
        return UserMessage(record["text"])
    if kind == "system_note":
        return SystemNote(record["text"])
    if kind == "tool_result":
        return ToolResult(record["call_id"], record["name"], record["content"])
    if kind == "assistant":
        calls = tuple(
            ToolCall(
                c["call_id"],
                c["name"],
                c.get("arguments"),
                c["raw_arguments"],
            )
            for c in record.get("tool_calls", ())
        )
        return AssistantMessage(record.get("text", ""), calls)
    raise RolloutError(f"unknown record type {kind!r}")
```

- **`_dump_item` 写 `{"type": "user", ...}`，`_load_item` 按 `type` 分发**
  ——四种 `HistoryItem` 变体一一对应。未知类型抛 `RolloutError`（正文
  §13 的"不是 rollout 文件"用同一套词）。
- **assistant 的加载用 `c.get("arguments")` 和 `c["raw_arguments"]`**——
  `arguments` 可能缺（用 `None`），`raw_arguments` 必须存在（`_migrate`
  保证 v1 文件也有它）。`ToolCall` 是位置参数构造（四个字段顺序固定）。

## P3 · `_add` / `replay`：六行 `isinstance` 分支

正文 §6 把语义讲透了，实现：

```python
def _add(history: History, item: HistoryItem) -> None:
    if isinstance(item, UserMessage):
        history.add_user(item.text)
    elif isinstance(item, SystemNote):
        history.add_system_note(item.text)
    elif isinstance(item, AssistantMessage):
        history.add_assistant(item.text, item.tool_calls)
    elif isinstance(item, ToolResult):
        history.add_tool_result(item.call_id, item.content)
    else:  # pragma: no cover
        raise AssertionError(item)


def replay(history: History, item: HistoryItem) -> None:
    """Put one stored item back into a live history, through the front door."""
    _add(history, item)
```

- **`_add` 是"把一条存储的 item 放回活的 History"的唯一入口**——通过
  `add_*` 方法而不是直接改 `history.items`，因为 `add_*` 会执行第 1 章的
  不变量检查（"每个工具调用恰好被回答一次"）。一个在 turn 中间断掉的
  rollout 不能变成一个非法对话（模块 docstring 明说："the same invariant
  that refuses to build one in memory refuses to load one from disk"）。
- **`replay` 只是 `_add` 的公开别名**——`_add` 是内部实现（`history()`
  和 `_size` 都用），`replay` 是第 4/6 章 import 的公开名字。

## P4 · `list_sessions` / `resolve` / `rollout_path`：路径与排序

正文 §17 自述"路径拼接和排序，`sessions` 命令的输出在 §14 展示过"。
完整实现：

```python
def list_sessions(directory: Path = DEFAULT_DIR) -> list[Rollout]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.jsonl"), reverse=True):
        try:
            out.append(read_rollout(path))
        except RolloutError:
            continue
    return out


def rollout_path(session_id: str, directory: Path = DEFAULT_DIR) -> Path:
    return Path(directory) / f"{session_id}.jsonl"


def resolve(reference: str, directory: Path = DEFAULT_DIR) -> Path:
    """Accept a path, a session id, or `last`."""
    if reference == "last":
        sessions = list_sessions(directory)
        if not sessions:
            raise RolloutError(f"no sessions in {directory}")
        assert sessions[0].path is not None
        return sessions[0].path
    candidate = Path(reference)
    if candidate.exists():
        return candidate
    candidate = rollout_path(reference, directory)
    if candidate.exists():
        return candidate
    raise RolloutError(f"no session {reference!r} (looked in {directory})")
```

- **`list_sessions` 按文件名倒序 = 最新在前**——`new_session_id` 是
  时间戳开头（`%Y%m%dT%H%M%S`），文件名排序就是时间排序（docstring 那句
  "listing a directory is listing a history"）。**读不了的跳过不抛**
  （`except RolloutError: continue`）——一个坏文件不该让 `sessions` 命令
  整体失败。
- **`resolve` 三种输入**：`"last"`（最新会话）、一个存在的路径、一个
  session id（拼 `rollout_path` 后查）。都失败就 `RolloutError` 并说明
  找过哪。`assert sessions[0].path is not None` 是给类型检查器的：
  `read_rollout` 总是设置 `path`，但类型上它可空。
- **`rollout_path` 三行**：`Path(directory) / f"{session_id}.jsonl"`。
  所有调用方共用这一个拼法，不会出现"有的地方 .jsonl 有的地方 .json"。

## P5 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| resume 后历史里缺了最后几轮 | 文件尾部损坏 | `read_rollout` 遇到 JSONDecodeError 就 `truncated_at` 停，`history()` 丢未完成的尾巴 |
| resume 后出现"孤立工具结果"错误 | 中间缺了一条 assistant 消息 | `history()` 里 `HistoryError` 就 break，已 settled 的部分仍是合法对话 |
| 两个进程写同一个会话文件 | 没锁 | `O_EXCL` 锁文件 + pid，冲突时点名 pid |
| `sessions` 命令被一个坏文件搞崩 | 读失败直接抛 | `list_sessions` 里 `except RolloutError: continue` |
| v1 旧文件 resume 后请求字节和原来不同 | `raw_arguments` 丢失 | `_migrate` 补 `raw_arguments`（`json.dumps(arguments)` 是最佳重建，明说不是原串） |
| `resolve("last")` 返回的不是最新的 | 排序不是按时间 | `new_session_id` 时间戳开头，文件名倒序即时间倒序 |
| 压缩过的会话 resume 后回到压缩前大小 | 重放了压缩前的旧消息 | `read_rollout` 遇到 `mark == "compacted"` 就 `items.clear()` |
| `from_json` 遇到未来版本的字段崩溃 | 直接 `cls(**payload)` | `known` 过滤未知键，丢弃不认识的 |
