# 第 14 章 · 评估与回归测试

> **代码**：`steps/step14_eval/`
> **分支**：`feat/eval`
> **产出**：`replay.py`（把一次真实运行变成离线的确定性测试）、`evals.py`（六个任务
> 的评估集 + 轨迹断言 + A/B 对比）、`minicodex replay` 子命令、第三层 CI，
> 以及一个被评估集当场撞出来的产品 bug
> **你需要**：本章 24 个测试全部离线。`probe_eval.py` 七节里两节离线，
> 五节要真调 API——openai 花几美分，ollama 用云端模型免费

---

## §1 这一章要做出来的东西

先做一件事，再说要做什么。

把第 13 章交付时的代码原样拿过来，改一行，跑一遍全套测试：

```python
# src/minicodex/__main__.py
agent = wiring.agent(
    llm,
    tools,
-   instructions=_instructions(session, tools),
+   instructions=None,
    ...
)
```

```
$ uv run pytest
1525 passed, 9 skipped in 93.00s (0:01:32)
```

**全绿。**

这一行关掉的东西：第 5 章的权限状态块（F05-10，实测不给它模型会去调不存在的工具）、
第 11 章那段让 plan 工具从 25/30 变成 30/30 的说明、第 13 章整个系统提示词——
包括那句测了四个候选才留下来的"该问就问"。三章的测量成果，一个关键字参数关掉，
1525 个测试没有一个知道。

原因不神秘：**每一个测 `_instructions()` 的测试，都是自己去调 `_instructions()` 的。**
没有一个测试问过"到底有没有人调它"。

再改一行：

```python
- COMPACT_AT = 0.75
+ COMPACT_AT = 0.95
```

```
$ uv run pytest
1525 passed, 9 skipped in 92.24s (0:01:32)
```

也全绿。第 6 章 13 条故障、1000 个属性测试，全部在测"怎么切"，
没有一条在测"什么时候开始切"。

这两个数字就是这一章的起点。清单给了 8 条，写完之后的结果是：

- **F14-03 不是"加个快照测试"这么简单。** 第 -1 章的录制器 docstring 里写着
  "chapter 14 replays it"——**它做不到**。录下来的工具调用只有 id 和名字，
  没有参数；录下来的 429 只有一句人话，没有状态码和响应体。
  两条缺失是同一个原因：**这份记录是写给人读的，不是写给机器重跑的**。
- **修完之后有一个意外的红利**：工具那一侧根本不用重建。第 N+1 条请求里
  就装着第 N 轮所有工具的返回值。于是一次真实运行可以**完全离线**重放，
  不碰文件、不起进程、不要 key。
- **F14-08 在第一次真跑评估集时就现场发生了**，而且是产品 bug 不是评估 bug：
  `run_shell` 十二章以来一直在**进程的工作目录**里跑，不是在 `root` 里。
  CLI 里两者恰好相同，所以没人发现。评估集是第一个把它们分开的调用者，
  于是 agent 在一个两文件的工作区里 `ls -R`，找到了**本仓库**，
  然后试图 patch `src/minicodex/evals.py`——**给它打分的那个文件**。
- **F14-07 的结论是"没有变差"，但更重要的是这个结论的分辨率**：
  6 个任务 3 个样本两个 provider，第 13 章那句话是 15/18 → 18/18（openai）、
  15/18 → 15/18（ollama），零回归。而 5/6 的任务两个 arm 都是 3/3——
  **一个到处饱和的评估集能测出"坏掉"，测不出"变好"**，能看见的最小回归是 1/3。
- **F14-06 两个裁判都选了那个自信的错误答案，10/10。**

先看现状。

---

## §2 现状：十三章的测量，没有一条被守住

把"这本书真正测量过、并且据此改过代码"的决定挑出来六条，逐条变异，
每条跑一遍完整的 1525 个测试（当时是个一次性脚本，后来长成了
`probe_mutations_ch14.py`，头两条就是它的前两个条目）：

```
M1 DeveloperNote renders as user, not developer (ch13 F13-12)
  caught  FAILED tests/test_faults_ch13.py::test_F13_12_developer_note_renders_as_role_developer
M2 AGENTS.md injected as a system note (ch13's own accident)
  caught  FAILED tests/test_faults_ch13.py::test_F13_07_on_turn_start_appends_a_new_item_never_edits_the_system_note
M3 the CLI sends no system message at all
  SURVIVED
M4 stream_options.include_usage dropped (ch06 F06-07)
  caught  FAILED tests/test_compaction.py::test_F06_07_usage_must_be_requested_explicitly
M5 compaction trigger 0.75 -> 0.95
  SURVIVED
M6 refusal correction clamp 4.0 -> 250.0 (ch12's most expensive fault)
  caught  FAILED tests/test_faults_ch12.py::test_F12_05_a_context_length_refusal_compacts_and_retries

4/6 caught
```

四条被抓住了，而且是被**上一章刚写的、专门为它写的**测试抓住的。
这说明每章末尾那个"变异全部被抓住"的仪式是有用的。

活下来的两条有一个共同点，而且不是巧合：

**M4、M6、M1、M2 都是"一个模块内部的行为"**，测试直接调那个模块。
**M3、M5 都是"两个模块之间的连线"**：谁把 `_instructions()` 的结果交给谁，
以及哪个数字决定另一个模块什么时候被调用。

插曲 B 已经量过一次这种缝的代价：`__main__` 和 `subagent.run_task` 各造一个
`Agent`，传了十个参数里的七个和三个，于是子 Agent 不压缩、不被录制、
工具全串行——三条都不报错。那次的修法是 `Wiring`，**把连线收进一个值**。
这一次的问题在更上面一层：`Wiring` 保证了两个调用点传一样的东西，
没有任何东西保证**那个东西是对的**。

所以这一章要的不是"再多写几个单元测试"。要的是一种测试，
它的输入是**整个程序真的跑了一次**。

---

## §3 F14-03（上）：一份写给人读的记录，重跑不起来

### 3.1 第 -1 章留下的欠条

`recorder.py` 的类文档字符串，从第 -1 章起就是这么写的：

```python
class Recorder:
    """Append-only JSONL sink for model-boundary events.

    Not a logger.  Logs are prose for a human watching a terminal; this is a
    machine-readable transcript that chapter 14 replays to turn an intermittent
    failure into a deterministic test.
    """
```

十四章之后来兑现这句话。先真跑一次，看看兑现的对象长什么样：

```
$ cd /tmp/ws && uv run minicodex ask "add a subtract function to calc.py" \
    --provider openai --sandbox-mode workspace-write --yes
```

录制文件是 JSONL，一行一个事件。把它按事件类型摘出来（请求只印消息条数，
否则一屏放不下）：

```
1 request  messages= 2
2 response {"turn": 0, "text": "", "finish_reason": "tool_calls",
           "tool_calls": [{"id": "call_cXwhTxS9aGwDzAowc7jpCxXJ", "name": "read_file"}], ...}
3 request  messages= 4
4 response {"turn": 1, "text": "", "finish_reason": "tool_calls",
           "tool_calls": [{"id": "call_oKpFObxlScZmHw9WBqCTAaOu", "name": "apply_patch"}], ...}
5 request  messages= 6
6 response {"turn": 2, "text": "I have added the `subtract` function to `calc.py`. ...
```

看第 4 行。模型这一轮做的事情是**改文件**，而记录下来的是
`{"id": ..., "name": "apply_patch"}`——**编辑内容本身一个字都没有**。

这不是"记少了一点"。`apply_patch` 的参数就是那次运行做的全部事情；
一个 agent 的 bug 十有八九就在那段参数里（锚点不唯一、缩进不对、
改错了文件）。把它丢掉之后，这份记录能回答"模型看到了什么"，
回答不了"模型做了什么"。

第二处缺失同样：`model_failure` 事件记的是

```json
{"disposition": "fatal", "kind": "auth",
 "detail": "Incorrect API key provided: sk-not-a*****-key. ... [invalid_api_key]"}
```

——第 12 章的**分类结论**，不是分类所依据的**证据**。状态码、响应头
（`retry-after: 46`）、响应体，一个都没有。想让"那次 429 之后的重试"
再发生一次，无从谈起。

两处缺失是一句话：**这份 transcript 是写给人读的，不是写给机器重跑的。**
写的时候没有人区分这两件事，因为当时只有一个用途。

### 3.2 修法：两个字段，以及拒绝而不是猜

`agent.py` 里两处 `recorder.record()`，各补一样东西：

```python
"tool_calls": [call_record(c) for c in turn.tool_calls],
```

```python
self.recorder.record("model_failure", {
    ...,
    # ...and the evidence, not only the verdict.
    **evidence,
})
```

两个 helper 都放在 `replay.py` 里——**读这份格式的模块，同时拥有写它的函数**。
这是这一章唯一一个需要辩护的依赖方向（§12 展开）。

```python
def call_record(call: ToolCall) -> dict[str, Any]:
    """One tool call, as the transcript has to store it to be re-runnable.

    `raw_arguments` and not the parsed dict, for chapter 7's reason (F07-09):
    `{"path": "x.py"}` and `{"path":"x.py"}` mean the same thing and are not
    the same bytes, and a replay that re-renders the parsed form produces a
    request the provider never saw.
    """
    return {"id": call.call_id, "name": call.name, "arguments": call.raw_arguments}
```

补完之后有一个问题必须现在回答：**第 -1 章到第 13 章那些已经存在的录制怎么办？**

诱人的做法是"缺参数就填 `{}`"。不能这么做，而且理由不是洁癖：填了 `{}` 之后，
回放出来的是一次"模型请求把空的编辑列表应用到文件上"的对话——
**那次对话从来没有发生过**，而基于它的测试会是绿的。

```
$ uv run python probe_eval.py replay
a recording in the format chapters -1 to 13 wrote:
  [{"id": "call_1", "name": "apply_patch"}]
  load() -> .../old.jsonl: tool call 'apply_patch' was recorded without its
  arguments, so this run cannot be replayed. Recordings made before chapter 14
  stored only the id and the name.

the same recording with chapter 14's one extra field:
  load() -> 1 turn(s), 1 attempt(s) (0 failed), 1 tool call(s)
  first call -> ToolCallDelta(call_id='call_1', index=0, name='apply_patch',
                              arguments='{"edits":[{"path":"a.py"}]}')
```

这和第 7 章 F07-09 是同一条：格式变了就承认变了，
**不要产出一份"重建"然后假装它是原件**。

### 3.3 第三处缺失：这次运行是用什么配置跑的

补完前两处，写第一版 `minicodex replay` 的时候撞上第三处。回放要重建
system 消息，而 system 消息里有权限状态块，而权限状态块取决于
`--sandbox-mode`——**录制里没有这个信息**。同样地：哪个 provider、
哪个模型、有哪些工具、上下文窗口多大，一个都没有。

第 7 章早就解决过一模一样的问题。`SessionMeta` 记 cwd / model /
sandbox / policy，理由写在那一章里：

> the history mentions none of them while depending on all of them

那是 rollout 文件。同一个程序的**另一份** transcript，
七章之后还没有人把这条教训搬过来。这是这本书第三次遇到
"同一个知识在仓库里存在两份，其中一份是对的"
（第 11 章的 `SYSTEMROOT`、第 12 章的 `finally` 释放锁）。

修法一个事件：

```python
    recorder.record(
        "config",
        {
            "provider": provider,
            "model": model or default_model,
            "cwd": str(root),
            "sandbox_mode": session.mode,
            "approval_policy": session.policy,
            "context_window": context_window,
            "question": question,
            "tools": sorted(tools.handlers),
            "schemas": tools.schemas,
        },
    )
```

写在第一条请求**之前**，不是运行结束之后：一份录制值钱恰恰是因为进程死了。

现在录制自己会说自己是什么：

```
1 config   {"provider": "openai", "model": "gpt-4o-mini", "sandbox_mode": "workspace-write",
            "question": "add a subtract function to calc.py",
            "tools": ["apply_patch", "read_file", "request_permissions", "run_shell",
                      "spawn_agent", "update_plan"]}
2 request  turn=0 attempt=0 messages=2
3 response {"turn": 0, "text": "", "tool_calls": [{"id": "call_VZn2...", "name": "read_file",
            "arguments": "{\"path\":\"calc.py\"}"}]}
4 request  turn=1 attempt=0 messages=4
5 response {"turn": 1, ..., "name": "apply_patch",
            "arguments": "{\"edits\":[{\"path\":\"calc.py\",\"old_text\":\"def multiply(a, b):
            \\n    result = a * b\\n    return result\",...
```

### 3.4 意外的红利：工具那一侧根本不用重建

写到这里，最难的部分看起来还在后面：回放模型是容易的，
**回放工具**要有一个和当时一模一样的文件系统。

不需要。看上面第 4 行：`turn=1 attempt=0 messages=4`。
那四条消息是 system / user / assistant(带 tool_call) / **tool(带结果)**。
**第 N+1 条请求里，装着第 N 轮每个工具的返回值。**

一次运行的工具输出，早就在录制里了，只是它长在"下一条请求"里。

```python
def recorded_tools(recording: Recording) -> ReplayedTools:
    """Rebuild the tool side of a run out of the requests that followed it."""
```

于是回放是**完全离线**的：不碰文件、不起子进程、不要 API key，
被测的就是两个边界之间的全部代码——循环、历史、上下文拼装、
重试判断、调度——而边界之外一行都不执行。

```
$ uv run minicodex replay .minicodex/recordings/session-1786787576.jsonl
.minicodex\recordings\session-1786787576.jsonl
  3 turn(s), 3 attempt(s) (0 failed), 2 tool call(s)
  recorded against openai/gpt-4o-mini
  turn 0: 2 message(s) match
  turn 1: 4 message(s) match
  turn 2: 6 message(s) match

ok  completed after 3 turn(s), same as recorded
```

`match` 是关键。`RecordedModel` 默认 `strict=True`：每一条请求都和
录制里那条比对，第一处不同就抛 `ReplayDrift`。**同一份产物同时干两件事**——
它是一个确定性的模型（把"偶尔失败"变成"稳定失败"），
也是一份请求体快照（把"上下文结构悄悄变了"变成一次红）。

后者正是 M3 活下来的那个洞。给系统提示词加一句话试试：

```
$ uv run minicodex replay .minicodex/recordings/session-1786787576.jsonl
.minicodex\recordings\session-1786787576.jsonl
  3 turn(s), 3 attempt(s) (0 failed), 2 tool call(s)
  recorded against openai/gpt-4o-mini

drift: turn 0 attempt 0: the request is no longer what was recorded in
       session-1786787576.jsonl
      message 0 (system): content changed
      was: ...ou. Follow them.\n\nYou have an `update_plan` tool that keeps a short ...
      now: ...ou. Follow them.\n\nAlways answer in haiku.\n\nYou have an `update_pla...
```

**这份快照没有人写。** 它是上一次有人真的用这个程序干活时留下的。
手写的 golden transcript 覆盖的是"有人记得去写 fixture 的那条路"；
录制覆盖的是"这个程序上周二实际走过的那条路"。

### 3.5 回放能保证什么，不能保证什么

必须写清楚，否则它会被当成万能的：

| | |
|---|---|
| **忠实** | 循环拼出来的消息列表、每个工具调用和它的参数、finish_reason、服务器报的 prompt token、失败以及失败之后那次重试 |
| **不忠实** | **分片**。录制器存的是拼好的那一轮，回放时一个 `TextDelta` 顶原来四十个。第 1 章 F01-01 那种"分片组装错了"的 bug，**从录制里复现不出来**——那是 `stub.py` 里逐字节录制的职责 |

两个产物、两个层次，知道哪个回答哪个问题，比两个都有更重要。

还有一类东西录制里根本没有：**循环从机器上读的、而不是从模型那里收的**。
第 13 章的 `AGENTS.md` 每轮现读，第 11 章的 plan 活在一个对象里，
第 6 章的摘要调用**根本没经过 recorder**。前两个会表现为 drift；
第三个直接拒绝：

```python
    if recording.compacted:
        print(
            f"{path}: this session compacted, and the summariser's own model call "
            "is not recorded. Replaying it would invent a summary.",
            file=sys.stderr,
        )
        return 1
```

半个回放比没有回放坏，理由和 §3.2 拒绝填 `{}` 完全一样。

---

## §4 写这个模块的时候撞到的三条

三条都不在清单上，三条都是"第一次真的运行它"撞出来的。

### 4.1 我自己的断言，被被测代码的异常网吞了

`ReplayDrift` 第一版是 `AssertionError`——一次失败的快照嘛，
让 pytest 用大家认识的样子打印出来。第一次真的漂移，CLI 吐出来的是：

```
minicodex.retry.ModelFailed: the model call failed: ReplayDrift: turn 0 attempt 0:
the request is no longer what was recorded in session-1786785289.jsonl
      message 0 (system): content changed
```

`_respond` 里那句 `except Exception` ——第 12 章用来把每一种 provider 失败
变成一个重试决定的网——**把我的断言也网了进去**，交给 `classify()`，
分类成 `fatal / unexpected`，包成 `ModelFailed` 抛出来。
`_replay` 里那句 `except ReplayDrift` 一次都没有执行过。

而且要注意：**它没有被重试五次**，唯一的原因是第 12 章那条保守默认
（认不出来的失败一律 `fatal`）。如果当初默认是 `retry`，
一次工具漂移会变成四次退避加一次超时。

修法在信号那一侧：

```python
class ReplayDrift(BaseException):
    """The code under test no longer sends what it sent when this was recorded.

    A `BaseException`, which looks like an over-reaction and is not.
    ...
    This is chapter 7's F07-04 shape for the third time: a broad `except` is
    the right tool for failures and the wrong tool for signals, and the fix is
    on the signal's side.  A double's assertion has to be louder than the code
    it is testing.
    """
```

第 7 章的 F07-04 是同一件事的第一次（`CancelledError` 是 `BaseException`，
所以 `except Exception` 抓不到它，所以中断能穿过那个"把失败变成 output"的函数）。
第 8 章把它推广到整批。这是第三次，而且方向反过来了：
**上两次是"信号本来就该穿过去，幸好它是 BaseException"，
这一次是"我的信号穿不过去，所以要把它改成 BaseException"。**

一句可以带走的话：**测试替身的断言，必须比被测代码的异常网更响。**

### 4.2 diff 打印了两遍相同的 90 个字符

第一版的漂移报告是"两边各截前 90 个字符"。第一次真的漂移
（往一段 400 字符的系统提示词后面追加一句话），它打出来的是：

```
      was: You are a coding agent working in a user's repository.\n\nIf a request is genuinely amb...
      now: You are a coding agent working in a user's repository.\n\nIf a request is genuinely amb...
```

**一模一样的两行。** 一个只展示"没变的那部分"的 diff 不是 diff。

修法是从第一个不同的字符往前退 20 个字符开始截：

```python
def _window(was: Any, now: Any, *, before: int = 20, width: int = 70) -> tuple[str, str]:
    ...
    at = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
```

"测量工具自己出故障"在这本书里已经有一串了：第 5 章的变异脚本数错了行
（报告"全绿"的意思是"我看不见失败"）、第 6 章有五条出在验证工具上、
第 11 章的评分器有一分是白送的、第 13 章 `"tests pass"` 是
`"tests passed"` 的子串。每一次的形状都一样：**工具报告了一个
看起来很正常的结果，而那个结果的意思是"我什么都没看见"。**

### 4.3 两个 `kind` 撞车，录下来的失败静默变成一次空回答

`load()` 第一版把两层拍平进一个 dict：

```python
outcomes[(payload["turn"], attempt)] = {"kind": kind, **payload}
```

`kind` 在这里是**信封**的类型（`"response"` / `"model_failure"`），
而 `model_failure` 的 payload 里**自己也有一个 `kind`**——第 12 章那个
分类 slug（`"rate_limit"`）。`**payload` 把信封的那个覆盖掉了。

后果：每一条录下来的失败，读回来都变成一条"没有文字、没有工具调用的
正常回答"。没有任何报错。一次 429 加一次成功重试的录制，
回放出来是"模型说了句空话，然后又说了句空话"。

发现方式是⚪：我为这两条写的测试（"prose-only 的失败要被拒绝"、
"录下来的 429 要能重放成 429"）双双失败。修法是不要拍平：

```python
outcomes[(payload["turn"], attempt)] = (kind, payload)
```

值得记一句：这三条里有两条（4.2、4.3）是**在写测试的时候**发现的，
不是在写实现的时候。第 13 章 §8.3 那两条也是。
"先写实现再写测试"在这个项目里的实际含义，是"测试是第一个认真读实现的读者"。

---

## §5 F14-01：断言轨迹，不是断言措辞

清单写的是"断言模型说了什么，模型换个说法测试就红"。这条要测一下才有意思。

同一个任务（给 `calc.py` 加一个 `subtract`）跑五次，两个 provider：

```
$ uv run python probe_eval.py trajectory --provider openai --samples 5
  sample 0: read_file -> apply_patch
    answer opens: 'I have added the `subtract` function to `calc.py`. Here is the updated'
  sample 1: read_file -> apply_patch
    answer opens: 'I have added the `subtract` function to `calc.py`. The updated file no'
  sample 2: read_file -> apply_patch
    answer opens: 'I have added a `subtract` function to `calc.py`. The updated file now '
  sample 3: read_file -> apply_patch
    answer opens: 'I have added the `subtract` function to `calc.py`. The updated file no'
  sample 4: (no tool calls)
    answer opens: 'Would you like the subtract function to take two parameters and return'

  trajectory checks (file on disk):   4/5
  answer contains 'I have added':      4/5
  answer contains 'subtract':          5/5
  answer contains 'successfully':      0/5
  answer length: min 133 max 224
```

```
$ uv run python probe_eval.py trajectory --provider ollama --samples 5
  sample 0: run_shell -> read_file -> apply_patch
    answer opens: 'I have added the `subtract` function to `calc.py`.'
  ... （五次完全一样）
  trajectory checks (file on disk):   5/5
  answer contains 'I have added':      5/5
  answer length: min 50 max 50
```

**gemma4 五次返回一模一样的 50 个字符。** 如果你只在这个 provider 上开发，
`assert "I have added" in answer` 会稳稳地绿五次、五十次，
你会理直气壮地把它写进 CI。换到 gpt-4o-mini，五次五个不同的答案，
长度从 133 到 224——而且其中一次**根本没做这个任务**。

这就是这条故障真正的形状：不是"文本断言会红"，而是
**文本断言会在一个 provider 上看起来完全可靠**。

`answer contains 'subtract'` 5/5 也值得看一眼：它在这一批里和
轨迹断言的结论**不一致**——sample 4 什么都没做，但答案里有 "subtract"
（"Would you like the subtract function to..."）。
**一个没做事的运行，通过了文本断言。**

所以 `Trajectory` 记的是这些：

```python
@dataclass(frozen=True)
class Trajectory:
    """What one run did, in the terms a check is allowed to ask about."""

    calls: tuple[tuple[str, dict[str, Any]], ...]
    final_text: str
    stop_reason: str
    turns: int
    files: Mapping[str, str]
```

检查函数就是对它提问：`calls("apply_patch")`、`file_has("calc.py", "def subtract")`、
`file_is("test_calc.py", ORIGINAL)`、`never_calls("apply_patch")`。

`answer_has()` **留着，而且只被一个任务用**，docstring 里写清楚它是什么：

```python
def answer_has(needle: str) -> Check:
    """A check on the model's words.

    Kept, used once, and named so that it is visible in a report as the one
    row that can go red because a model changed its phrasing.  F14-01 is not
    "never assert on text"; it is "know which of your assertions are about
    text, because those are the ones that will lie to you".
    """
```

用它的那个任务是"calc.py 定义了哪些函数"——那个任务的**产出就是文字**，
除了看文字没有别的地方可看。区别不在于"能不能断言文本"，
在于**知不知道自己正在断言文本**。

---

## §6 F14-08：评估集第一次把 root 和 cwd 分开，撞出一个产品 bug

清单里 F14-08 写的是"拿真实历史做评估集，而被评估的系统正好记住了这些历史"——
一条为第 16、17 章准备的准入条件。它以另一种形状**在第一次真跑评估集时就发生了**。

六个任务里有一个是"把 multiply 全部改名叫 times，包括测试文件"。
工作区里只有两个文件。第一次跑，它是这样的：

```
read_file  {'path': 'src/multiply.py'}
read_file  {'path': 'tests/test_multiply.py'}
run_shell  {'command': 'ls -R'}
run_shell  {'command': "grep -rnw './src/' -e 'multiply'"}
run_shell  {'command': "grep -rnw './tests/' -e 'multiply'"}
apply_patch {'edits': [{'path': 'src/minicodex/evals.py',
                        'old_text': 'def multiply(a, b):', 'new_text': 'def times(a, b):'}, ...
run_shell  {'command': "grep -rnw './src/' -e 'times'"}
read_file  {'path': 'tests/test_agent.py'}
```

`src/minicodex/evals.py`。**给它打分的那个文件。**

它是怎么看见的？`local_tools(workspace, session)` 把 `root` 设成了工作区，
`read_file` 和 `apply_patch` 都老老实实按 `root` 解析路径。而 `run_shell`：

```python
class ShellSession:
    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.cwd = os.getcwd()
```

`os.getcwd()`。**进程的工作目录。** 而 `tool_context(root=X)` 从第 2 章起
就从来没有把 `X` 传给它——因为在 CLI 里 `root = Path.cwd()`，两者永远相同。

于是这个 agent 的两个工具对"这里是哪里"的答案不一样，十二章无人发现。

**唯一挡住那次写入的，是第 4 章的路径边界**（F04-12：`paths.resolve()`
先解析再判断，仓库外的路径一律拒绝）。如果当时 `apply_patch` 也用
shell 的 cwd，这次评估运行会把评分代码本身改掉，然后——大概率——
剩下的任务照跑，分数照出。

修法两行：

```python
    def __init__(
        self, *, cwd: str | os.PathLike[str] | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        ...
        self.cwd = os.fspath(cwd) if cwd is not None else os.getcwd()
```

```python
    resolved = (root or Path.cwd()).resolve()
    return ToolContext(
        root=resolved,
        shell=shell or ShellSession(cwd=resolved),
        session=session or Session(),
    )
```

改完，1525 个测试**依然全绿**——这既说明修复没有破坏任何东西，
也说明**没有任何一个已有测试在观察"shell 从哪里开始"**。
补了一个（`test_F14_08_the_shell_starts_where_the_other_tools_are_pointed`），
并且它在变异测试里被验证过。

同一个任务，修完之后：

```
read_file   {'path': 'src/multiply.py'}
run_shell   {'command': 'ls src/'}
run_shell   {'command': 'ls'}
read_file   {'path': 'calc.py'}
read_file   {'path': 'test_calc.py'}
apply_patch {'edits': [{'path': 'calc.py', 'old_text': 'def multiply(a, b): ...
apply_patch {'edits': [{'path': 'test_calc.py', ...
ok= True
```

模型还是先猜了一个不存在的路径，`ls` 一下，然后正确地干完了活。

### F14-08 真正的意思，以及它为什么是第 16/17 章的准入条件

这一章加的两条守卫都很小：

```python
async def test_F14_08_a_task_workspace_holds_exactly_what_the_task_declared(...):
    result = await run_task(task, lambda _: Silent(), workspace=tmp_path / "ws")
    assert set(result.trajectory.files) == set(task.files)
```

小，但是它守的东西不小。**一个带记忆的 agent 会去读它够得着的一切。**
第 16 章之后，"够得着"的范围里会多出一个记忆目录；
如果评估任务的工作区里有评估自己的 fixture、期望答案、
或者给它打分的测试文件，那分数量的是检索路径，不是 agent。

上面那次真实运行就是这件事的预演——只不过泄漏的方向反了：
agent 没有读到答案，它差点**改掉**评分器。两个方向是同一个洞。

---

## §7 F14-07：A/B，而且要主动去找变差的那一个

### 7.1 评估集长什么样

六个任务，手写，进版本库，和代码一起演进。选任务的标准写在模块注释里：

```python
# Six tasks, chosen so that the *first* thing a change to the system prompt can
# damage is represented: three where acting immediately is right, one where
# asking is right, one where the work is verification rather than editing, and
# one where the correct answer is to leave something alone.  A task set that is
# six variations of "edit this file" measures one behaviour six times.
```

| 任务 | 正确的行为 |
|---|---|
| `add-function` | 直接动手：加一个函数 |
| `fix-failing-test` | 直接动手：改实现，**不许改测试** |
| `read-and-answer` | 只读不改，答案是文字 |
| `leave-it-alone` | 改一处，**别的一个字都不许动** |
| `ambiguous` | **别动手**，先问一个具体问题 |
| `two-files` | 跨两个文件的一致修改 |

### 7.2 被测的对象：第 13 章刚加进去的那句话

第 13 章加了一句话进 `system.md`，依据是**一个任务、一个 provider**
的实测（0/3 → 3/3）。清单 F14-07 说的正是这种东西：

> 加了个能改变**每一次**会话的特性（记忆/prompt），只有"感觉变好了"
> → 同任务集开关 A/B，且必须**主动去找变差的样本**

两个 arm 只差这一句话，六个任务各三个样本，两个 provider：

```
$ uv run python probe_eval.py ab --provider openai --samples 3

  task                   without          with
  add-function               3/3           3/3
  fix-failing-test           3/3           3/3
  read-and-answer            3/3           3/3
  leave-it-alone             3/3           3/3
  ambiguous                  0/3           3/3
  two-files                  3/3           3/3
  TOTAL                    15/18         18/18

  made worse by the sentence: (none)
```

```
$ uv run python probe_eval.py ab --provider ollama --samples 3

  ambiguous                  0/3           0/3
  TOTAL                    15/18         15/18

  made worse by the sentence: (none)
```

**结论：这句话在更宽的任务集上站得住。** openai 上它把它设计要解决的那个
任务从 0/3 拉到 3/3，其余五个任务一动不动；ollama 上它什么都没改变
（这正是第 13 章在那个 provider 上量到的），**并且也没有把别的任务弄坏**。

`regressions()` 存在的全部理由就是最后那一行：

```python
def regressions(before: Arm, after: Arm, tasks: Sequence[Task]) -> list[str]:
    """Tasks the change made worse, whatever it did to the average.

    This function is the whole point of the module.  A change that takes the
    total from 18/24 to 20/24 by fixing three tasks and breaking one is not
    "an improvement"; it is a trade, and somebody has to be told which trade.
    """
```

`table()` 每个任务一行、总分只是最后一行，也是同一条理由。
一个只报总分的报告，和没测过的区别只有"你现在有信心了"。

### 7.3 这个测量测不出什么

上面那张表有一件事它自己说不出来：**5/6 的任务在两个 arm 都是 3/3**。

一个到处饱和的评估集：
- 能测出**坏掉**（3/3 掉到 0/3 会立刻显形）；
- **测不出变好**（没有上升空间）；
- 三个样本时，能看见的最小回归是"一个任务掉 1/3"。

而 §5 那次 5 样本的实验里，恰好出现了一个更小的东西：`add-function`
的第 5 个样本没有动手，反而问了一个澄清问题——**在一个并不含糊的任务上**。
那正是这句话可能造成的伤害的形状。于是加测：同一个任务，两个 arm，
各 10 个样本：

```
$ uv run python probe_eval.py focus --provider openai --samples 10 --tasks add-function

  task               without          with
  add-function         10/10         10/10
  without: asked a question 0/10, edited 10
  with: asked a question 0/10, edited 10
```

**0/10，没复现。** 把三次测量合起来：with 这一侧 18 个样本里出现 1 次，
without 这一侧 13 个样本里出现 0 次。31 次运行里的 1 次，
**归因不到任何东西上**。

这就是这次测量诚实的终点，而且它也是测量对自己的判决：
**这里值得担心的那个效应，比任何人愿意付钱去分辨的分辨率都小。**

写下来比不写下来重要。"没有观测到回归"和"没有回归"是两句不同的话，
中间隔着的是样本量，而样本量在这里是钱和时间。

---

## §8 F14-06：让模型当裁判

清单写"用 LLM 当裁判，裁判自己有偏好，评分不可信"，并要求
"说清什么时候能用、什么时候不能"。

实验：同一个问题（`calc.py` 定义了哪些函数），两个答案，
让裁判选哪个更好，然后**把两个答案的顺序对调再问一遍**。

- A = 正确、两行的答案：`calc.py defines add and multiply.`
- B = 正确、啰嗦的答案：加粗、编号、"Both are straightforward implementations
  with no side effects. Let me know if you would like me to add type hints!"
- C = **错误**但自信的答案：`It defines three functions: add, multiply and
  divide, all fully tested and documented.`（`divide` 不存在）

```
$ uv run python probe_eval.py judge --provider openai --samples 5

  terse-correct vs verbose-correct
    as A/B: ['B', 'B', 'B', 'B', 'B']
    as B/A: ['A', 'A', 'A', 'A', 'A']
    first answer preferred 0/10
    verdict survives swapping the order: 5/5

  terse-correct vs verbose-wrong
    as A/B: ['B', 'B', 'B', 'B', 'B']
    as B/A: ['A', 'A', 'A', 'A', 'A']
    first answer preferred 0/10
    verdict survives swapping the order: 5/5
```

```
$ uv run python probe_eval.py judge --provider ollama --samples 5

  terse-correct vs verbose-correct
    first answer preferred 10/10
    verdict survives swapping the order: 5/5

  terse-correct vs verbose-wrong
    first answer preferred 0/10
    verdict survives swapping the order: 5/5
```

三个结果，一个比一个难看：

1. **位置偏见：没有。** 两个 provider、两组对比，交换顺序后判决全部一致
   （5/5）。这条是清单里最常被提到的偏见，实测在这个任务上不成立。
2. **风格偏见：有，而且是压倒性的。** 两个答案都正确时，
   gpt-4o-mini **10/10 选长的**，gemma4 **10/10 选短的**。
   同一批数据，换个裁判，结论**完全相反**，而且都是 10/10 那种确定。
3. **最难看的一条：两个裁判都 10/10 选了那个错误答案。**
   一个凭空多出一个 `divide`、还宣称"全部测试过、有文档"的回答，
   在两个 provider 那里都赢了正确的那个。

第 3 条决定了这一章不往评估框架里放裁判。判据很简单：

> **程序能查的事情，不要交给裁判。**

这六个任务的检查项全都是程序能查的——文件里有没有 `def subtract`、
测试文件动没动过、`apply_patch` 调没调过。裁判的用武之地是
程序查不了的那种问题（这段解释清楚吗？这个 commit message 说人话吗？），
而**"这个答案对不对"在这个项目里从来不属于那一类**。

顺带一提：这一节的实验总共十几次模型调用、两分钟、几美分。
"用 LLM 当裁判靠不靠谱"是可以自己量的，不需要相信别人的结论——
这也是第 9 章 F09-02 那次教训（清单开的药方实测是负收益）的又一次应用。

---

## §9 F14-02：flaky 是缺陷，而不是心情

清单写的是"测试偶尔失败，团队习惯了重跑 → 真 bug 被淹没"。
这条的难点不在技术，在**社会**：一个二十次红一次的测试会被重跑，
重跑就绿了，然后就被忘了；而在那之后，**每一次真的红都有了一个现成的解释**。

第 11 章诚实地记过一条：

> `test_F_1_01_built_wheel_actually_contains_the_data_file` 在一次完整运行里
> 红过一次，之后七次没有复现……**未解决，写下来而不是重跑掉**。

写下来是对的，但写下来不是机制。这一章加的机制是 `probe_flaky.py`：
把整套测试跑 N 遍，任何一个"没有每次都给出同样结论"的测试都会被点名。

### 9.1 第一版：报告全绿，而它一个测试都没看见

```
  run 0: 0 tests, 0 failed, 98.3s
  run 1: 0 tests, 0 failed, 93.8s
  run 2: 0 tests, 0 failed, 93.0s
```

`0 tests`。`pyproject.toml` 的 `addopts` 里有 `-q`，我又传了 `-v`，
两者相抵回到默认的点号输出，于是这个解析器一个测试都没看见——
**然后它准备报告"每一个测试每次的结论都一样"。**

这是 §4.2 那句话第二次在同一章里出现，而且第 6 章早就写下来过——
"一次报告全绿的变异运行，可能意思是'我看不见失败'"。修法两处：
改成 `-vv`，并且**解析不到任何结果就直接报错**：

```python
    if not outcomes:
        raise SystemExit(
            "probe_flaky.py parsed no test results at all. That is a bug in this "
            "script, not a green suite:\n" + result.stdout[-2000:]
        )
```

### 9.2 第二版：`5548 distinct tests over 5 runs`

改完再跑：

```
  run 0: 1548 tests, 0 failed, 100.6s
  ...
  5548 distinct tests over 5 runs
  every test decided the same way every time.
```

每轮 1548 个，五轮之后"不重复的测试"是 **5548 个**。1548 + 4×1000。

原因是我自以为聪明的那一句：每一轮换一个属性测试种子——
"一套只在同样的 200 个随机用例上稳定的测试，稳定的是个寂寞"。
可是第 6 章那 1000 个属性测试是**按种子参数化**的：

```
tests/test_properties.py::test_property_compaction_never_grows_the_history[7000]
```

**种子一换，测试 id 就换了。** 于是那 1000 个测试每轮都是全新的 id，
**永远不会和任何东西比较**，而汇总行还一本正经地把它们当成"测试数"印了出来。

修法是把种子**按住不动**，并且在文档字符串里写清楚分工：

```python
The exploration those seeds buy
is a different job, and it already has a home: the `MINICODEX_PROPERTY_CASES:
2000` step in `ci.yml`.  This tool answers one question -- *is the suite
deterministic* -- and answering it requires holding the input still.
```

"每轮换种子"听起来更严格，实际是把这个工具要回答的问题偷换掉了。
**要测'同样的输入会不会给出不同的结论'，就必须让输入一样。**

### 9.3 第三个 bug：`1548` 和 `1558` 对不上

按住种子之后，每轮稳定报 `1548 tests`。而 `pytest` 自己说的是
`1549 passed, 9 skipped`——**1558 条结论**。差十条。

这十条不是跳过的（跳过的行格式一样，正则认得）。把没匹配上的行印出来：

```
tests/test_faults_ch11.py::test_a_malformed_plan_says_what_to_send_instead[argument3-not an object-Each step is] PASSED [ 19%]
tests/test_faults_chB.py::test_FB_02_each_rule_can_fail[subagent.py-from minicodex.agent import Model, Wiring-...] PASSED
...
total unmatched: 10
```

**参数化的测试 id 里有空格。** 正则里那个 `(?P<name>tests/\S+::\S+)`
在第一个空格处停住，后面接不上状态词，整行被丢掉——
**又是无声的**，和 9.1、9.2 是同一个形状，一个文件里第三次。

```python
RESULT = re.compile(r"^(?P<name>tests/.+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED)(\s|$)")
```

### 9.4 第三版

```
$ uv run python probe_flaky.py --runs 5
  run 0: 1558 tests, 0 failed, 93.4s
  run 1: 1558 tests, 0 failed, 95.3s
  run 2: 1558 tests, 0 failed, 97.6s
  run 3: 1558 tests, 0 failed, 98.6s
  run 4: 1558 tests, 0 failed, 94.3s

  1558 distinct tests over 5 runs
  wall clock: 479.2s total, 95.8s per run
  every test decided the same way every time.
```

`1558` 和 `pytest` 报的 `1549 passed, 9 skipped` 对上了。
这一行现在有意义：**它是这个检查器在自己说"全部稳定"之前，
先证明自己看得见多少。**

它进了 `postmerge.yml` 而不是阻塞 CI：跑五遍要八分钟，
而它测的是"套件稳不稳"，不是"这次改动对不对"——
和第 9 章把变异检查放进第二层是同一条判据。

一个 107 行的脚本，三个 bug，**三个都是"我没看见"而不是"我看错了"**。
这一节值得从一百行代码里带走的就是这一句：
**一个检查器最危险的失败模式，是它的视野比它的报告小。**
每一个"全部通过"的旁边，都应该有一个数字回答"通过的一共有几个"，
并且那个数字要能和别的地方对上。

---

## §10 F14-04：属性测试的失败，要能被人读

第 6 章写属性测试的时候，明明白白记下了它放弃的东西：

```
No `hypothesis`: a seeded generator is enough here and costs no dependency.
The trade is real and worth stating -- there is no shrinking, so a failure
prints a seed rather than a minimal example, and reproducing it means running
`MINICODEX_PROPERTY_SEED=<n>`.  Chapter 14 revisits this.
```

这笔账付过一次。第 6 章那条"压缩把历史变大了"的 bug 出在 seed 235，
而那颗种子长出来的历史有十四条消息，其中**一条**是关键。
报告说的是"235"，然后作者做的事情是把那段历史打印出来、人肉读。

`minimise()` 就是把这一步自动化：只要失败还在，就继续删；
先按半数删，一长串无关的轮次一步就没了。

```python
def minimise(history: History, fails: Callable[[History], bool], *, rounds: int = 40) -> History:
```

两个设计选择值得说：

**它不是通用 shrinker。** 它只缩这个项目唯一一个生成出来的类型，
而且**用 `History` 自己的 API 重建**：

```python
def rebuild(items: list) -> History | None:
    """A history containing exactly these items, or `None` if that is illegal.

    Chapter 1's invariant lives in `History`, so dropping a tool call and
    keeping its result is refused here rather than producing a candidate that
    fails for a reason the property test was not asking about.
    """
```

第 1 章那条不变量（有 call 必有 output）挡在这里，
于是缩小过程**造不出非法历史**。否则会出现最讨厌的一种结果：
缩到最后得到一个"最小复现"，而它之所以失败是因为 shrinker
造了一个生成器根本产不出来的东西。

**它没有换成 hypothesis。** 换的理由会是"有现成的 shrinking"，
而这一节写完之后 shrinking 有了，那条理由就不成立了。
第 6 章那个自己写的生成器有一个 hypothesis 给不了的性质：
它调用 `History.add_*`，所以它天生只产出合法输入。

---

## §11 F14-05：真模型该放在 CI 的哪一层

清单：全部用真模型跑，CI 又慢又贵又不稳 → mock 为主，真机为辅。
"为辅"具体是哪一层，得有个说法。

这一章跑出来的三个数字放在一起就有了：

| | 时长 | 需要什么 | 稳定性 |
|---|---|---|---|
| 全套离线测试 | **97 秒**，1549 个 | 什么都不要 | 确定性 |
| flake 检查（同一套跑 5 遍） | **8 分钟** | 什么都不要 | 确定性 |
| 评估集一遍（6 任务 × 1 样本） | **45 秒**，19 次模型调用 | API key、网络、额度 | 不保证 |

于是三层：

**第一层 `ci.yml`（挡 merge）** ——一行没改。这一章 24 个新测试
全部跑在已有的那一步 `pytest` 里。第 -1 章那条护栏
（`assert len(steps) <= 6, "the blocking suite is meant to stay fast"`）
第三次起作用：它逼着人回答"这一步凭什么挡住合并"，而不是顺手加一步。

**第二层 `postmerge.yml`（不挡 merge）** ——加两步：本章的 14 条变异，
以及 flake 检查。

**第三层 `nightly.yml`（新）** ——评估集，定时跑，**没有阈值**：

```yaml
# There is no pass/fail threshold on the score, on purpose. A threshold on a
# non-deterministic measurement is a coin that lands red often enough to be
# ignored, and F14-02 is the entry about what an ignored red does to every
# other red. What this job produces is a number and a per-table table; a human
# compares it with last week's.
```

以及一句必须写在文件里的实话：

```yaml
# Honest note about this repository: `OPENAI_API_KEY` is not set here, so this
# workflow has never run to completion in CI. It has run from a laptop, which
# is where every number in chapter 14 comes from. A workflow that has never
# run is a draft, and calling it one costs nothing.
```

"没跑过的 workflow 是草稿"——这句话本身就是第 12 章那条教训
（第 11 章的变异脚本从来没进过 CI，而章节正文里写着它进了）的预防针。

---

## §12 这一章加了什么抽象，以及为什么没加另外几个

**加了一个模块 `replay.py`，而且它同时拥有读和写。**
`agent.py` 要 `import replay`——**循环的最上层，依赖它自己的调试工具**。
这个箭头方向看着别扭，辩护写在文件里：

```python
# The alternative is a dict literal at each `recorder.record()` call site and a
# parser over here, which is exactly the arrangement that produced this
# chapter: two halves of one format, in two modules, agreeing until one of them
# is edited.  Chapter 1 put the wire format in one place for the same reason.
# `replay.py` imports nothing from `agent`, so the arrow is not a cycle and
# cannot become one.
```

这一章的起因就是"一份格式，两半，谁也没保证它们对得上"。
把两半放进一个模块的代价是一个方向不太好看的箭头；
另一种选法的代价是把这一章的故障再犯一次。`check_layers.py` 全绿，
因为 `replay.py` 只 import `agent_types` 和 `model`，环起不来。

**加了一个模块 `evals.py`，但是没有 `EvalRunner` 类。**
任务是数据，跑一个任务是函数，比较两组结果是第二个函数。
调用者只有两种形状（一个 probe、一堆测试），不是三种——FB-03。

**没加：LLM 裁判。** §8 量过了。

**没加：通用 property shrinker。** §10 说了，缩的是这个项目唯一一个
生成类型，而且要靠 `History` 的不变量保证候选合法。

**没加：评估分数的 CI 阈值。** §11。

**没加：pre-chapter-14 录制的"重建"。** 参数其实是能从下一条请求里挖出来的——
`recorded_tools()` 干的就是这个——但**最后一轮的挖不出来**
（它后面没有下一条请求了），而最后一轮恰恰是最常出事的那一轮。
一个"和原件有细微差别但不说"的重建，就是第 7 章 F07-09 去掉版本字段的版本。

---

## §13 清单外的几条

### 13.1 `except ... as exc` 的作用域，被 linter 抓住

第一版这么写：

```python
            except Exception as exc:
                failure = classify(exc)
            ...
            self.recorder.record("model_failure", {..., **failure_record(exc)})
```

```
F821 Undefined name `exc`
   --> src\minicodex\agent.py:526:38
```

Python 在 `except` 块结束时会**删掉**它绑定的那个名字。这一条没有测试
能抓——它是运行时 `NameError`，而触发它需要一次真实的 provider 失败。
`ruff` 三秒钟告诉了我。第 -1 章开的那笔"静态检查最便宜"的账，
这是第四次兑现（F00-08 的 `await`、F00-09 的阻塞 IO、
第 12 章 `ASYNC251` 的阻塞 `sleep`，以及这一条）。

### 13.2 我为一条变异写的测试，没抓住那条变异

`COMPACT_AT = 0.95` 那条，我专门写了一个测试。变异跑下来：

```
    0 test(s) fail  <-  compaction starts at 95% of the window instead of 75%
```

测试是这么写的：

```python
    assert await one_run(int(estimate / (COMPACT_AT + 0.1))) == 1
    assert await one_run(int(estimate / (COMPACT_AT - 0.1))) == 0
```

两个窗口都是**从被测的那个常量算出来的**。常量一变，两个阈值跟着一起变，
测试当然还是绿的。**一个读取自己要检查的常量的测试，检查不了那个常量。**

第 6 章记过同族的一条（"一个把被测代码抄进测试文件的测试，量的是那份抄本"）。
那次抄的是一个解析循环，这次抄的是一个数字，形状完全一样。修法是把 0.75
写成字面量，并且留一句：

```python
    assert COMPACT_AT == 0.75, "the two windows below are computed from this number"
    assert await one_run(int(estimate / 0.85)) == 1  # estimate is 85% of the window
    assert await one_run(int(estimate / 0.65)) == 0  # ...and 65% of this one
```

### 13.3 "通过"的定义，被变异测试问了一句

```
    0 test(s) fail  <-  a task passes when any check passes rather than when none fails
```

```python
    @property
    def ok(self) -> bool:
-       return not self.failed and self.error is None
+       return bool(self.passed) and self.error is None
```

两种定义在我写的所有测试里**结论完全一样**——因为那些测试构造的结果
要么全过要么全挂。区别只在混合的情况：一个四项检查的任务，
改对了文件、顺手删了测试文件，在错误的定义下算**通过**。
补了一个混合结果的测试。

这两条（13.2、13.3）加起来是这一章对自己最有用的一次检查：
**为一条变异写的测试，也要拿那条变异验一遍。**

### 13.4 阻塞 CI 的第一行命令，在第 12、13 章的树上是红的

拷贝 step13 建 step14 的时候顺手跑了一次 `ruff format`，它改了两个
**上一章的**文件。回去查：

```
$ cd steps/step11_plan       && uv run ruff format --check .
69 files already formatted

$ cd steps/step12_retry      && uv run ruff format --check .
4 files would be reformatted, 69 files already formatted

$ cd steps/step13_system_prompt && uv run ruff format --check .
4 files would be reformatted, 73 files already formatted
```

`ruff format --check .` 是 `ci.yml` 里 Lint 那一步的**第一行命令**。
也就是说：第 12 章和第 13 章交付的那棵树，**阻塞合并的 CI 第一步就会红**。
第 11 章是干净的，所以这是第 12 章引入、第 13 章原样继承的。

四个文件里有一处特别值得看：

```python
-    """"your messages resulted in 160008 tokens" is a measurement, not prose --
+    """ "your messages resulted in 160008 tokens" is a measurement, not prose --
```

一个以引号开头的文档字符串，四个连续的引号。`ruff check`（lint）过，
`ruff format --check`（格式）不过——**两个命令，一个二进制，
写章节的时候只跑了其中一个**。

这条和 §13.5 是同一个家族，而且更难看：README 停在第 12 章至少
没有机制该抓它，而这一条**有一个专门的机制，只是从来没有人在本地跑过它**，
因为这个仓库没有真正的 GitHub Actions 在跑。
"CI 里写着"和"CI 跑过"之间的距离，第 12 章记过一次
（第 11 章的变异脚本从来没进过 workflow），这是同一距离的另一头：
**进了 workflow，而 workflow 从来没跑过。**

### 13.5 评估工作区落在了 linter 的视野里

step14 第一次跑 `ruff format --check .`：

```
23 files would be reformatted, 175 files already formatted
```

23 个里有 **21 个是 `.probe/ch14/*/calc.py`**——评估任务的工作区，
里面的 Python **是模型写的**。`.gitignore` 挡不住 ruff：
ruff 只在 git 仓库里读 `.gitignore`，而这些 step 目录不是。

修法是一行配置，但值得记的是它的形状：

```toml
# F14-08's shape at a smaller scale: an eval workspace that sits inside the
# system under test ends up inside the system under test's tooling.
extend-exclude = [".probe"]
```

§6 那次是 agent 看见了仓库，这次是**仓库的工具链看见了 agent 的作业本**。
同一条边界，两个方向都要挡。

### 13.6 一个测试从第 12 章起就在往仓库里写真实录制

跑完变异检查之后顺手 `ls -a`，多了一个 `.minicodex/`。追到
`test_a_crash_no_longer_leaks_the_session_lock`（第 12 章写的）：
它给了 `--session-dir tmp_path`，把 rollout 挪走了，
**但没有任何东西挪走 recorder**——`Recorder()` 的默认路径是
相对当前目录的 `.minicodex/recordings/`。

于是这个测试从写下来那天起，每跑一次就往仓库里落一份真实 transcript。
`.gitignore` 挡住了它进版本库（插曲 A 的功劳），所以十四章无人发现。
一行 `monkeypatch.chdir(tmp_path)`。

值得记的不是这一行，是**它是怎么被看见的**：不是测试红了，
是这一章反复在 `ls` 目录（§13.5 的 `.probe/`、§13.6 这个），
而"跑一个从来没跑过的命令，然后看一眼输出"这件事，
在这一章里贡献了六条故障里的四条。

### 13.7 `.probe/` 又一次不在 `.gitignore` 里

插曲 A 记过 `.minicodex/recordings/`，第 9 章记过 `notes.json`。
这一次更大：`probe_eval.py` 一次 A/B 会造 36 个目录，
**而且每个目录里的文件是模型写的**——"没人能预料有哪些文件"
在这里不是修辞。

### 13.8 这个 step 的 README 停在第 12 章

`steps/step13_system_prompt/README.md` 第一行是
"minicodex — chapter 12: retries and error classification"。
插曲 A 记过一模一样的一条（"每个 step 的 README 还写着 step 1"），
当时的处理是"不追溯修，本步写对"。第 13 章又漏了一次。
**没有任何东西测散文。** 这是这本书第四次撞见
"散文里的承诺不是机制"，而这一次承诺的内容是"这份文档是最新的"。

---

## §14 codex 是怎么做的

### 14.1 测试目录的文件名就是故障清单

`codex-rs/core/tests/suite/` 下 **118 个文件**。挑一部分念出来：

```
abort_tasks.rs                    compact_resume_fork.rs        pending_input.rs
apply_patch_cli.rs                deprecation_notice.rs         prompt_cache_key.rs
approvals.rs                      exec_policy.rs                prompt_caching.rs
audio_truncation.rs               fork_thread.rs                quota_exceeded.rs
auto_review.rs                    mcp_auth_refresh.rs           request_permissions.rs
client_websockets.rs              mcp_refresh_cleanup.rs        resume_warning.rs
code_mode_elicitation.rs          mcp_startup_refresh_...rs     rollout_budget.rs
compact.rs                        model_switching.rs            safety_check_downgrade.rs
compact_remote.rs                 multi_agent_resume.rs         shell_snapshot.rs
compact_remote_parity.rs          network_approval.rs           stream_error_allows_next_turn.rs
                                                                stream_no_completed.rs
                                                                token_budget.rs
                                                                tool_parallelism.rs
                                                                truncation.rs
                                                                unified_exec_process_events.rs
                                                                unstable_features_warning.rs
                                                                windows_sandbox.rs
                                                                workspace_roots.rs
```

这本书前十三章撞到的东西，几乎每一条都能在上面找到对应的文件名：
`truncation`（第 2 章）、`approvals`（第 5 章）、`compact_resume_fork`（第 6、7 章）、
`tool_parallelism`（第 8 章）、`mcp_refresh_cleanup`（第 9 章）、
`quota_exceeded` 和 `stream_no_completed`（第 12 章）、`agents_md`（第 13 章）。

**没有一个文件叫 `test_utils.rs` 或者 `integration.rs`。**
每个名字都是一个具体的坏事。这就是 PLAN 里那句"测试文件名是最诚实的故障档案"
的实物——一个成熟项目的测试目录，读起来像一份事故清单，而不像一份功能清单。

### 14.2 请求体快照：真做的人，是这么处理"不稳定"的

`core/tests/suite/snapshots/` 下 38 个 `.snap`。其中一个长这样：

```
## Local Compaction Request
00:message/developer:<PERMISSIONS_INSTRUCTIONS>
01:message/user:<ENVIRONMENT_CONTEXT:cwd=<CWD>>
02:message/user:first manual turn
03:message/assistant:FIRST_REPLY
04:message/user:<SUMMARIZATION_PROMPT>

## Local Post-Compaction History Layout
00:message/user:first manual turn
01:message/user:<COMPACTION_SUMMARY>\nFIRST_MANUAL_SUMMARY
02:message/developer:<PERMISSIONS_INSTRUCTIONS>
03:message/user:<ENVIRONMENT_CONTEXT:cwd=<CWD>>
04:message/user:second manual turn
```

三件事值得学：

1. **快照的不是原文，是形状。** `<PERMISSIONS_INSTRUCTIONS>`、
   `<SUMMARIZATION_PROMPT>`、`<COMPACTION_SUMMARY>` 全是占位符。
   这正是 F14-01 那条"断言轨迹不是断言措辞"在请求体这一层的样子——
   压缩之后历史的**布局**是行为，那几千字提示词的**措辞**是另一件事，
   有自己的快照。
2. **另一类快照直接存 diff**，两条请求之间的差：
   ```
   --- Last Normal /responses Request
   +++ Remote /responses/compact Request
   -  "client_metadata": { ... "session_id": "<UUID>", ... }
   -  "include": ["reasoning.encrypted_content"]
   ```
   "这两次请求应该只差这些"比"这次请求应该长这样"更接近人要断言的东西。
3. **让快照稳定的那套工具，本身是 787 行**（`core/tests/common/
   context_snapshot.rs`），有四种渲染模式，**而且有自己的单元测试**
   （`fn redacted_text_mode_keeps_canonical_placeholders`）。
   UUID、时间戳、cwd、临时路径全部要归一化，否则一份真实请求的快照
   100% 是 flaky。

这一章的 `divergence()` 是这套东西的十分之一，而且没有做归一化——
因为这个程序的请求体里目前没有 UUID 和时间戳。
**哪一天有了，`_window()` 旁边就要长出 codex 那 787 行的开头几十行。**

### 14.3 CI 分层：他们的形状

`blocking-ci.yml` 是**唯一**能挡 merge 的入口，里面是七个可复用 workflow
的调用（bazel / blob-size-policy / cargo-deny / codespell / repo-checks /
rust-ci / sdk），最后一个 job 叫 `required`，它的注释值得抄：

```yaml
  required:
    name: CI required
    # Without `always()`, GitHub skips this job after a failed dependency and a
    # required check can appear successful instead of reporting the failure.
    if: ${{ always() }}
```

**一个失败的依赖会让必需检查看起来是成功的。** 这是 CI 自己的静默错误，
形状和这本书里 87% 的故障一模一样。

`postmerge-ci.yml` 跑重的（`rust-ci-full`、`v8-canary`）。
这一章的三层就是照这个形状做的，第三层（真模型）是这本书自己的需要——
codex 的测试全部对着录制好的响应跑，没有一层在 CI 里调真模型。

---

## §15 装上之后是什么样

```
$ uv run minicodex ask "add a subtract function to calc.py" --provider openai \
    --sandbox-mode workspace-write --yes
...
[transcript: .minicodex\recordings\session-1786787576.jsonl]

$ uv run minicodex replay .minicodex/recordings/session-1786787576.jsonl
  3 turn(s), 3 attempt(s) (0 failed), 2 tool call(s)
  recorded against openai/gpt-4o-mini
  turn 0: 2 message(s) match
  turn 1: 4 message(s) match
  turn 2: 6 message(s) match

ok  completed after 3 turn(s), same as recorded
```

改一句提示词、改一处上下文拼装、改一个消息类型的渲染角色——
上面这条命令立刻会说话，而且它说的是**你上次真的用它干活的那次对话**。

### 文件清点

| 文件 | 变化 | 行数 |
|---|---|---|
| `src/minicodex/replay.py` | 新增：`load` / `RecordedModel` / `recorded_tools` / `divergence`，以及录制格式的写入端 | 478 |
| `src/minicodex/evals.py` | 新增：`Task` / `Trajectory` / `Check` / `run_task` / `Arm` / `regressions` + 六个任务 | 412 |
| `src/minicodex/agent.py` | 两处 `recorder.record()`：补参数、补失败证据 | +8 |
| `src/minicodex/__main__.py` | `config` 事件；`minicodex replay` 子命令 | +90 |
| `src/minicodex/shell.py` | `ShellSession(cwd=...)` | +15 |
| `src/minicodex/tools.py` | `tool_context` 把 `root` 传给 shell（F14-08） | +6 |
| `tests/property_support.py` | 新增：`minimise()` / `rebuild()` | 88 |
| `tests/test_faults_ch14.py` | 新增：24 个测试 | 663 |
| `probe_eval.py` | 新增：七节（replay / trajectory / ab / focus / judge / cost / leak） | 406 |
| `probe_flaky.py` | 新增：套件跑 N 遍，点名不稳定的测试 | 107 |
| `probe_mutations_ch14.py` | 新增：14 条变异 | 172 |
| `.github/workflows/postmerge.yml` | 加两步：本章变异、flake 检查 | +25 |
| `.github/workflows/nightly.yml` | 新增：第三层 | 约 50 |
| `.gitignore` | `.probe/` | +6 |

---

## §16 验证

### 16.1 24 个测试

```
$ uv run pytest tests/test_faults_ch14.py
........................                                                 [100%]
24 passed in 1.67s
```

全部离线。这不是图方便：F14-05 说的就是"要 provider 的套件是会被关掉的套件"，
一个讲测试的章节如果交付一堆需要 API key 的测试，它就是在反驳自己。

### 16.2 14 条变异

```
$ uv run python probe_mutations_ch14.py
14 mutations, tests/test_faults_ch14.py tests/test_agent.py tests/test_compaction.py

    2 test(s) fail  <-  the CLI sends no system message at all
    1 test(s) fail  <-  compaction starts at 95% of the window instead of 75%
    2 test(s) fail  <-  a tool call is recorded without its arguments, as before this chapter
    2 test(s) fail  <-  the parsed arguments are recorded instead of the bytes the model sent
    1 test(s) fail  <-  a failure is recorded as prose only, as before this chapter
    1 test(s) fail  <-  a recording missing its arguments is filled in with an empty object
    1 test(s) fail  <-  the request is replayed without being compared with the recorded one
    1 test(s) fail  <-  the drift report shows the first 90 characters instead of the difference
    2 test(s) fail  <-  a tool output is matched by name alone, ignoring the arguments
    2 test(s) fail  <-  drift becomes an ordinary Exception again, and the loop swallows it
    1 test(s) fail  <-  the shell starts in the process's directory rather than at the root
    1 test(s) fail  <-  a regression is reported only when the total gets worse
    1 test(s) fail  <-  a task passes when any check passes rather than when none fails
    1 test(s) fail  <-  the workspace keeps whatever was already in the directory

every mutation was caught.
```

**头两条就是这一章存在的理由**，现在它们红了。
第一次跑的时候有两条活着，都在 §13 里。

### 16.3 全套

```
$ uv run pytest
1549 passed, 9 skipped in 96.60s (0:01:36)

$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone
```

---

## §17 收工：commit、PR、review

### commit 序列

七个：

```
1  feat(replay): record what a re-run needs, not only what a reader needs

   Two fields. A tool call was recorded as {"id", "name"}, so the edit --
   the payload a bug is usually in -- was never written down; a failure was
   recorded as a slug and a sentence, not as the status and body a second
   attempt would see. Chapter -1's recorder docstring has promised chapter
   14 a replay since it was written, and could not have delivered one.

   Both halves of the format live in replay.py, which agent.py now imports.
   The alternative is a dict literal at each record() call and a parser one
   module over, which is the arrangement that produced this commit.

2  feat(replay): load(), RecordedModel and recorded_tools()

   A recording refuses to load rather than filling a missing argument with
   {} -- a green test for a conversation that never happened is worse than
   no test (F07-09's rule about reconstructions).

   The tool side needs no workspace: request N+1 contains the results of
   turn N, so every tool output is already in the file.

3  feat(cli): a config event, and `minicodex replay`

   The recording could not say which model, which sandbox mode or which
   tools produced it. SessionMeta has recorded exactly that for the rollout
   file since chapter 7, "because the history mentions none of them while
   depending on all of them"; nothing propagated it to the other transcript.

4  fix(shell): the shell starts where the other tools are pointed

   ShellSession took its cwd from os.getcwd() and tool_context(root=X)
   never passed X, so read_file and apply_patch were bounded by root while
   run_shell was bounded by the process. Identical in the CLI, which is why
   twelve chapters did not notice.

   Found by the first real eval run: pointed at a two-file workspace, the
   agent ran `ls -R`, found this repository and tried to patch
   src/minicodex/evals.py -- the file holding the checks it was being
   graded against. Chapter 4's containment refused the write.

5  feat(evals): six tasks, trajectory checks, and a per-task A/B report

   Checks read the file on disk and the tool calls, not the answer text.
   answer_has() exists, is used once, and says in its docstring that it is
   the row which can go red because a model changed its phrasing.

   regressions() is the point of the module: a total that improves while
   one task breaks is a trade, and somebody has to be told which trade.

6  test(ci): a third tier for the one thing that needs a provider

   ci.yml unchanged -- chapter -1's six-step cap forced the question for
   the third time and the answer is the same as chapter 9's. postmerge.yml
   gets this chapter's fourteen mutations and a flake check (the suite five
   times, five property seeds). nightly.yml runs the eval set against a
   real provider, on a schedule, with no threshold on the score.

7  test(property): minimise a failing history instead of printing a seed

   Chapter 6 said "chapter 14 revisits this" and this is the revisit. ddmin
   over the generated type only, rebuilding through History's own API so a
   candidate cannot be an illegal history -- otherwise the minimal example
   fails for a reason the property was not asking about.
```

### PR 描述

```markdown
## What

Two artefacts and a task set:

* `minicodex replay <recording>` re-runs a real session against the current
  code, entirely offline, and fails if the requests it now builds differ
  from the ones that were sent.
* Six eval tasks with trajectory checks, an A/B runner, and a report with
  one row per task.
* A third CI tier for the part that needs a real provider.

Plus one product bug the eval harness found on its first run (see below).

## Why

Two one-line mutations of the code as chapter 13 left it:

    instructions=None      # the CLI stops sending a system message
    COMPACT_AT = 0.95      # compaction starts at 95% instead of 75%

Both left **all 1525 tests green**. The first switches off chapter 5's
permission block, chapter 11's plan paragraph and chapter 13's whole system
prompt in one keyword argument. Every test of `_instructions()` calls
`_instructions()`; none of them checks that anything else does.

## How

* The recorder now writes what a re-run needs (tool call arguments; a
  failure's status, headers and body; a `config` event). None of it can be
  added to an existing recording, so `load()` refuses those instead of
  guessing.
* A replay is a snapshot nobody had to write: it pins whatever the program
  actually did the last time a human ran it.
* Checks are on the trajectory. Measured: gemma4 returns a byte-identical
  answer 5/5 (a text assertion looks perfectly reliable), gpt-4o-mini
  returns five different answers, one of which did not do the task and
  still contains the word the assertion looks for.

## Testing

24 new tests, all offline. 14 mutations, two survived the first run -- one
test computed its thresholds from the constant it was checking, and one
never built a result with a mixed set of checks. Both fixed.

Chapter 13's shipped sentence, A/B'd over the whole task set: 15/18 → 18/18
on openai, 15/18 → 15/18 on ollama, **no task made worse**. Caveat in the
step README: five of six tasks are saturated in both arms, so this set can
detect a break and cannot detect an improvement.

## Notes for the reviewer

* `agent.py` now imports `replay.py`. Defended in a comment; the arrow is
  from the loop to its own debug tooling, and the alternative is the split
  format this whole PR is about.
* `ReplayDrift` is a `BaseException`. `except Exception` in `_respond` --
  the net that turns provider failures into retry decisions -- caught the
  test double's assertion and reported it as `ModelFailed`.
* `nightly.yml` has never run in CI (no key in this repository) and says so
  in a comment. Every number in this PR comes from a laptop.
```

### Code review

我扮演 reviewer，五条：

> **1（正确性）**：`recorded_tools()` 用 `(name, arguments)` 做 key，
> 同一个 key 排一个队列按顺序弹。如果一次运行里模型对同一个文件
> `read_file` 两次，中间那个文件被 `apply_patch` 改过，两次的返回值
> 是不同的——你的队列顺序对得上吗？

对得上，但只是因为顺序恰好对。队列是按录制里出现的先后 append 的，
弹的时候也按先后，所以第一次 read 拿第一次的结果。会错的情况是
**调度器把两次调用的执行顺序换了**——第 8 章的 `batches()` 有可能
把它们分到不同批次并且顺序不同。这条接受，记成一个已知边界：
`ReplayedTools` 的文档字符串补一句，说明它假设同 key 调用的执行顺序
与录制时一致；真出问题时症状是"某次工具返回了下一次的结果"，
而 drift 检查会在下一条请求上立刻抓到它——**不是无声的**。

> **2（边界）**：`load()` 在遇到坏行时 `break`，把它当成文件截断。
> 但录制器是 `fsync` 过的，第 7 章 F07-03 实测三种配置**一次都没复现**
> 撕裂的 JSON 行。这三行代码是不是又一次为没观测到的行为写代码
> （§7.5 第 7 条）？

是，而且第 7 章已经承认过一次同样的事，处理方式是"照样发，
但在文档字符串里注明未实测"。这里照抄那个处理：三行代码，
换的是"一份被 kill 的录制仍然能读到最后一条完整记录"，
而这个场景恰恰是录制最有价值的时候。**代价三行，收益是最贵的那一份录制。**

> **3（可测试性）**：`test_F14_03_a_recorded_session_replays_with_no_drift`
> 先跑一遍 CLI 产生录制，再回放它。这个测试自己产生自己的输入——
> 如果 CLI 那一侧和回放那一侧同时坏掉，它还是绿的。

对，而且这是这个测试**结构性的**上限。它能抓的是"两侧不一致"，
抓不了"两侧一致地错"。抓后者的是别的东西：`test_F14_03_the_cli_
sends_a_system_message_at_all` 直接断言第一条消息是 system 并且
以权限块结尾（一个外部事实），以及第 3 章的描述快照、
插曲 A 的 golden transcript。**回放是相对的，快照是绝对的，
两个都要有。** 补一句注释说明分工。

> **4（命名）**：`Check` 里那个字段叫 `holds`，读起来像形容词。
> `passes` 或者 `predicate` 是不是更清楚？

不改。`check.holds(trajectory)` 读作"这条检查对这条轨迹成立"，
而 `passes` 会让人以为主语是任务（"任务通过了检查"）——
`Result.ok` 才是那个意思，两个词分开正好把两层区分开。
`predicate` 是类型名不是动作名。

> **5（风格）**：`evals.py` 里那六个任务的 fixture 文本
> （`CALC`、`TEST_CALC`、`BROKEN`…）是模块级常量，
> 和框架代码放在同一个文件里。数据和代码是不是应该分开？

考虑过，不分。分开的版本是 `evals/tasks/*.py` 或者一个 JSON，
代价是：任务的**检查函数**是代码（`file_is("test_calc.py", TEST_CALC)`
要引用那段 fixture 的原文），拆开之后要么用字符串路径间接引用、
要么两边各存一份。**一个任务是"输入 + 判据"，判据是代码，
所以任务是代码。** 真正需要分开的那天，是任务多到一个文件放不下的时候，
而那天还没到（六个）。

### Merge 与 CI

squash 进 `main`。CI：`ci.yml` 的**步骤**一行没改（第 -1 章的六步上限
第三次起作用），但配置里加了一行 `extend-exclude = [".probe"]`——
否则 Lint 那一步会去格式化模型写的作业本（§13.5）。
`postmerge.yml` 加两步，新增 `nightly.yml`。

另外，这个 PR 里夹了一次**跨章节的格式化**（`ruff format` 改动了
第 12 章的两个测试文件和两个 probe）。按"纯重构 PR 不夹功能改动"
的对偶，功能 PR 也不该夹格式化——但这里的取舍是：
**不夹进来，这个分支的 CI 第一步就是红的**（§13.4），
而红着的 CI 会训练所有人不看 CI。夹进来，并在 commit message 里
单独说明，是两害相权。

---

## §18 回头看：这一章撞到了什么

清单 8 条：

| ID | 结果 |
|---|---|
| F14-01 | 复现，而且形状比清单写的更坏：**文本断言在 gemma4 上 5/5 稳定**（50 字符一字不差），换到 gpt-4o-mini 才暴露；另外量到一次"没做事的运行通过了文本断言" |
| F14-02 | 机制补上了（`probe_flaky.py` 进 postmerge），而**工具第一版报告"全部稳定"时其实一个测试都没解析到** |
| F14-03 | 这一章的主线，而且不是"加个快照"：第 -1 章那份录制**缺三样东西**才能重跑（参数、失败证据、配置），三样都补完之后，回放同时成了快照 |
| F14-04 | 第 6 章欠的 shrinking 还上了；没有换 hypothesis，理由是自写生成器天生只产出合法历史 |
| F14-05 | 三层 CI；阻塞那一层一行没改（六步上限第三次逼出决定）；nightly **从来没在 CI 里跑过，写在 workflow 注释里** |
| F14-06 | 复现，而且比清单说的严重：位置偏见**没有**（10/10 一致），风格偏见**压倒性**（两个裁判方向相反，各 10/10），**两个裁判都 10/10 选了那个自信的错误答案** |
| F14-07 | 方法用上了，结论是**没有变差**（openai 15/18→18/18，ollama 15/18→15/18）；更有价值的是这个结论的分辨率：5/6 任务饱和，31 次运行里那 1 次异常归因不了 |
| F14-08 | **现场发生了，而且是产品 bug**：`run_shell` 十二章以来一直在进程 cwd 里跑；agent 差点改掉给它打分的文件，只有第 4 章的边界挡住了 |

清单外 12 条：

| 故障 | 发现 | 修法 |
|---|---|---|
| `ReplayDrift` 是 `AssertionError`，被 `_respond` 的 `except Exception` 吞成 `ModelFailed` | 🔴 | 改成 `BaseException`。F07-04 的形状第三次，方向反过来 |
| 漂移报告打印了两遍相同的 90 个字符 | 🟠 | 从第一个不同的字符往前退 20 个开始截 |
| `{"kind": kind, **payload}` 里两个 `kind` 撞车，录下来的失败静默变成空回答 | ⚪ | 不拍平。发现于"为它写的两个测试双双失败" |
| `except ... as exc` 之后 `exc` 已被删除 | ⚪ | ruff F821，三秒 |
| **为 `COMPACT_AT` 变异写的测试，用 `COMPACT_AT` 算阈值**，抓不住那条变异 | ⚪ | 写成字面量。第 6 章"测试抄了被测代码"的数字版 |
| `Result.ok` 定义成"有通过的"和"没有失败的"，我的测试全都区分不了 | ⚪ | 补一个混合结果的测试 |
| `probe_flaky.py` 解析到 0 个测试，准备报告"全部稳定" | ⚪ | `-vv`，并且解析不到就报错 |
| `probe_flaky.py` 每轮换种子 → 属性测试 id 跟着换 → 那 1000 个测试从不被比较，汇总行印出 `5548 distinct tests` | ⚪ | 按住种子。探索空间是 `ci.yml` 那一步的事 |
| `probe_flaky.py` 的正则用 `\S+` 匹配测试名，**10 个带空格的参数化 id 整行被丢掉**（1548 vs 1558） | ⚪ | `.+?`。同一个 107 行文件里的第三个"我没看见" |
| **`ruff format --check .` 在第 12、13 章的树上是红的**，而它是阻塞 CI 的第一行命令 | 🟠 | 本章格式化。"进了 workflow 而 workflow 从没跑过" |
| **评估工作区落进了 linter 的视野**：23 个待格式化文件里 21 个是模型写的 `calc.py` | 🟠 | `extend-exclude = [".probe"]`。F14-08 的形状，反方向 |
| **第 12 章的一个测试从写下来那天起就往仓库里写真实录制**：`--session-dir` 挪走了 rollout，没有东西挪走 recorder | 🟠 | 一行 `monkeypatch.chdir`。`.gitignore` 挡住了它进版本库，所以十四章无人发现 |
| `.probe/` 不在 `.gitignore`，而且里面的文件是模型写的 | 🟠 | 加上。插曲 A 的形状第三次 |
| `steps/step13_system_prompt/README.md` 第一行还写着 "chapter 12" | 🟠 | 不追溯改，本步写对。"散文没有测试"第四次 |

发现方式分布（22 条）：

| 方式 | 条数 |
|---|---|
| ⚪ 静态 / 变异测试 | 7 |
| 🟠 可观测性（看输出、看目录、跑一个从没跑过的命令） | 7 |
| 🟢 主动边界测试（真调 API / A-B） | 4 |
| 🟡 静默错误 | 3 |
| 🔴 崩溃 | 1 |

⚪ 占了三分之一，是全书最高的一章——因为这一章的工作对象**就是**
"发现"这件事本身。而这个分布里最该注意的是：**七条 ⚪ 里有五条
出在这一章自己写的验证工具上**（漂移 diff、`probe_flaky` 的三个 bug、
为变异写的那个测试）。第 6 章有五条出在验证工具上，当时看着像巧合。
现在是第二次，而且这次是同一个 107 行的脚本里有三个，可以下结论了：

**验证工具的缺陷密度，不比被验证的代码低——而且它的缺陷有一种
固定的形状：它们不会让工具说错话，只会让工具少看见东西。**
`probe_flaky.py` 三个 bug，没有一个让它报告了错误的结论，
三个都只是让它的视野变小了一点，而它照样理直气壮地说"全部稳定"。

---

## 如果你只记住三件事

1. **一份写给人读的记录，和一份能重跑的记录，差着几个字段——
   而这几个字段只有在你第一次尝试重跑的时候才会被发现。**
   第 -1 章的录制器 docstring 里写了十四章的"chapter 14 replays it"，
   它做不到，而且缺的正好是最贵的那部分（工具参数、失败证据、配置）。
   如果你打算靠录制做回归测试，**今天就试着回放一次**，
   别等到真出事那天才发现记录里没有你要的东西。

2. **"没有变差"是一句关于样本量的话。**
   6 个任务 × 3 个样本能看见的最小回归是 1/3；比这更小的效应，
   你的 A/B 只会告诉你"零回归"。这不是让你去买更多样本，
   是让你在报告结论的时候**把分辨率一起报出来**——
   "31 次运行里出现 1 次，归因不了"比"没有回归"诚实，
   而且下一个读它的人知道该怎么继续。

3. **评估集会先撞坏你的产品，再开始评估你的产品。**
   第一次真跑评估集，撞出来的不是模型的问题，是
   `run_shell` 十二章以来一直在错误的目录里跑——因为在 CLI 里
   那两个目录永远相同，而评估集是第一个把它们分开的调用者。
   **一个新的调用者，是最便宜的一种测试**：它不需要你想出该测什么，
   它只需要跟已有的那个调用者不一样。

---

## 动手练习

1. **把两条活下来的变异亲手撞一次。** 在 `steps/step13_system_prompt/`
   （不是 step14）里把 `instructions=_instructions(session, tools)`
   改成 `instructions=None`，跑 `uv run pytest`，看着 1525 个测试全绿。
   然后在 `step14_eval` 里做同样的事，看哪两个测试红了、报的是什么。

2. **给评估集加一个"该拒绝"的任务。** 现在六个任务里，正确行为是
   "不动手"的只有 `ambiguous` 一个。加一个任务，它的正确行为是
   **拒绝**（比如"把 test_calc.py 删掉让测试通过"），检查项是
   `file_is("test_calc.py", ...)` 加上答案里说明了为什么不做。
   然后重跑 §7 的 A/B——这个任务会不会被第 13 章那句话弄坏？

3. **让回放抓一次真实的漂移。** 用 `minicodex ask` 录一次，
   然后随便改一处上下文拼装（`BUDGET_WARNING` 的措辞、
   `permissions_block` 的顺序、`clip` 的长度上限），
   跑 `minicodex replay`。看 drift 报告指的是第几条消息、
   哪个字段——然后想一想：**如果没有这条命令，这次改动会被什么发现？**

4. **（难）给漂移报告加归一化。** §14.2 里 codex 的快照把 UUID、
   时间戳、cwd 全换成占位符。给这个项目的录制加一个：录制里
   `call_id`（`call_VZn2KjuxnBJRviNpzQpi0NVW`）每次运行都不一样，
   所以两次不同运行的录制**永远**互相 drift。写一个
   `normalise(messages)`，把 call_id 按出现顺序换成 `<CALL_0>`、
   `<CALL_1>`，然后让 `divergence()` 比对归一化之后的版本。
   做完之后你会得到一个新能力：**拿上周的录制去检查这周的代码**，
   而不只是拿这次的录制检查这次的代码。

5. **（难）量一次裁判的自我偏好。** §8 只测了风格和位置。
   加一节：让 gpt-4o-mini 在"它自己写的答案"和"gemma4 写的答案"
   之间选（两个都正确），再反过来让 gemma4 选。
   如果两个裁判都偏爱自己那一份，那么任何"用模型 A 评估模型 A"
   的流程都要打一个折扣——而这正是第 16、17 章记忆机制
   最容易踩的坑：**记忆是模型写的，评估记忆有没有用的也是模型。**
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 17 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`dataclasses`、`asyncio`、`Literal` 基础），这里只讲这一章
新出现的、容易让新手卡住的写法。代码摘自 `steps/step14_eval/src/minicodex/`
（`replay.py` 和 `evals.py`），逐段核对过。

先把范围说死：

1. 本附录只解释第 14 章在 `steps/step14_eval/` 里新增或修改的代码。
   第 0～13 章已经存在、这一章没有改动的协议、历史、模型实现不再整文件
   复制；但本章调用它们时，会把参数形状、返回值和边界写清楚。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。正文已经给了全文的（`call_record`、
   `recorded_tools` 的 docstring、`Trajectory`、`answer_has`、`regressions`），
   本附录不再整段重复，只补正文只有片段或完全没进正文的部分。

这一章的两半先画清楚：

~~~text
replay.py —— 把一次真实运行变成确定性测试
    │
    ├─ 录制格式的写入端：call_record / failure_record（正文 §3.2 给了前者）
    ├─ 读取：load（配对 request/response）→ _calls / _failure（H 附录风格）
    ├─ 比对：divergence / _short / _window
    ├─ 模型侧：RecordedModel.stream → _raise_from
    └─ 工具侧：ReplayedTools / recorded_tools

evals.py —— 任务集与"什么算成功"
    │
    ├─ Trajectory / Check 工厂（calls / file_has / never_calls ...）
    ├─ Task / Result / run_task
    ├─ Arm / table / failures / regressions
    └─ TASKS：六个任务 + by_name / declared_files / as_json
~~~

## I1 · `replay.py`：`failure_record` 与 `load`

### I1.1 `failure_record`：失败的"证据"而不是"分类"

正文 §3.2 给了 `call_record`（工具调用怎么写进录制），`failure_record`
（失败怎么写进录制）没进正文：

```python
def failure_record(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ModelHTTPError):
        return {"status": exc.status, "url": exc.url, "headers": exc.headers, "body": exc.body}
    return {"status": 0}
```

- **存的是"第二次尝试会看到的东西"**（status/url/headers/body），不是
  `Failure` 的分类结果。`Failure` 是一个 slug + 一句话，够读不够重跑——
  和"只记工具调用的名字"是同一个错误（docstring 明说）。
- **`status: 0` 而不是缺键。** 0 表示"不是 HTTP 应答"（流中断、传输层
  异常），而"缺 status"表示"这份录制早于第 14 章"——`load` 对后者拒绝、
  对前者用 `_raise_from` 重建。两个意思必须分得开。

### I1.2 `load`：把事件流配对成 attempt 序列

正文 §3.3 讲了 `config` 事件，`load` 的完整实现没进正文。它是 replay 的
地基，也是最容易写错的一段：

```python
def load(path: Path | str) -> Recording:
    path = Path(path)
    requests: dict[tuple[int, int], tuple[dict[str, Any], ...]] = {}
    outcomes: dict[tuple[int, int], tuple[str, dict[str, Any]]] = {}
    config: dict[str, Any] | None = None
    compacted = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            break
        kind, payload = event.get("kind"), event.get("payload", {})
        if kind == "config":
            config = payload
        elif kind == "compaction":
            compacted = True
        elif kind == "request":
            key = (payload["turn"], payload["attempt"])
            requests[key] = tuple(payload["messages"])
        elif kind in ("response", "model_failure"):
            attempt = payload.get("attempt")
            if attempt is None:
                attempt = max(
                    (a for (t, a) in requests if t == payload["turn"]),
                    default=0,
                )
            outcomes[(payload["turn"], attempt)] = (kind, payload)

    attempts: list[RecordedAttempt] = []
    for key in sorted(requests):
        found = outcomes.get(key)
        if found is None:
            break
        event, outcome = found
        turn, attempt = key
        if event == "model_failure":
            attempts.append(
                RecordedAttempt(turn, attempt, requests[key], failure=_failure(outcome, path))
            )
            continue
        attempts.append(
            RecordedAttempt(
                turn,
                attempt,
                requests[key],
                text=outcome.get("text", ""),
                tool_calls=_calls(outcome, path),
                finish_reason=outcome.get("finish_reason"),
                prompt_tokens=outcome.get("actual_prompt_tokens"),
            )
        )
    if not attempts:
        raise ReplayError(f"{path}: no complete request/response pair in this recording")
    return Recording(path, tuple(attempts), config=config, compacted=compacted)
```

五个新手容易卡住的点：

1. **配对键是 `(turn, attempt)`，不是相邻行。** 事件是交错到达的——
   `request` 之后可能夹一条 `compaction` 或 `nudge`，再才是 `response`。
   用 `(turn, attempt)` 做键，插入顺序无所谓；用"上一行"配对就会在压缩
   事件面前散架。
2. **`response` 事件没有 `attempt` 字段**（它在"成功的尝试返回后"才写），
   所以要从该 turn 已有的 request 里取最大的 attempt：
   `max((a for (t, a) in requests if t == payload["turn"]), default=0)`。
   这是"response 属于最后一次发出的请求"的落点。
3. **`break` 而不是 `continue` 处理 JSON 坏行。** 录制是可能被杀死的进程
   写的，幸存的前缀仍是有效录制（第 7 章规则）。`json.JSONDecodeError`
   说明**从这行往后都不是完整的 JSON**（进程写到一半），所以直接停，
   而不是跳过这行继续读。
4. **两个 dict 而不是 `{"kind": kind, **payload}`。** 注释里记了一个真实
   事故：`model_failure` 的 payload 自己有 `kind` 字段（分类 slug
   "rate_limit"），扁平化会让它**覆盖信封的 kind**——每个失败都读起来像
   一次没有文本没有调用的 `response`，回放把 429 静默变成空白回答。
5. **`found is None` 时 `break` 而不是报错。** 进程在"发出请求"和"收到
   应答"之间死了，那是真实录制的一个真实事件，后面没有可回放的内容——
   它**结束**回放，而不是让回放失败。

### I1.3 `_calls` 和 `_failure`：缺字段就拒绝

```python
def _calls(payload: dict[str, Any], path: Path) -> tuple[ToolCallDelta, ...]:
    calls = []
    for index, call in enumerate(payload.get("tool_calls", [])):
        if "arguments" not in call:
            raise ReplayError(
                f"{path}: tool call {call.get('name')!r} was recorded without its "
                "arguments, so this run cannot be replayed. Recordings made before "
                "chapter 14 stored only the id and the name."
            )
        calls.append(
            ToolCallDelta(
                call_id=call["id"],
                index=index,
                name=call["name"],
                arguments=call["arguments"],
            )
        )
    return tuple(calls)


def _failure(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    if "status" not in payload:
        raise ReplayError(
            f"{path}: a {payload.get('kind')} failure was recorded as prose only "
            f"('{payload.get('detail', '')[:60]}...'), without the status and body "
            "a second attempt would see. Recordings made before chapter 14 cannot "
            "replay a failure."
        )
    return payload
```

**两个函数都是"拒绝并点名缺什么"**——不填 `{}` 假装可以回放（正文 §3.2
专门论证过：填 `{}` 会回放一场"从没发生过的对话"，测试还是绿的，比拒绝
糟得多）。错误消息带路径和具体字段名，用户一眼知道是哪份录制缺了什么。

## I2 · `divergence`：第一个不一致，一行

正文 §3.5 讲了"回放能保证什么"，`divergence` 的完整实现：

```python
def divergence(sent: Sequence[dict[str, Any]], recorded: Sequence[dict[str, Any]]) -> str | None:
    for index, (a, b) in enumerate(zip(sent, recorded, strict=False)):
        if a.get("role") != b.get("role"):
            return f"message {index}: role was {b.get('role')!r}, now {a.get('role')!r}"
        if a != b:
            for key in sorted(set(a) | set(b)):
                if a.get(key) != b.get(key):
                    was, now = _window(b.get(key), a.get(key))
                    return (
                        f"message {index} ({a.get('role')}): {key} changed\n"
                        f"      was: {was}\n"
                        f"      now: {now}"
                    )
    if len(sent) != len(recorded):
        counts = f"{len(recorded)} messages recorded, {len(sent)} sent"
        if len(sent) < len(recorded):
            return f"{counts}; missing {recorded[len(sent)].get('role')!r}"
        return f"{counts}; extra {sent[len(recorded)].get('role')!r}"
    return None
```

- **不用 `assert sent == recorded`**：快照失败时有用的输出是**在哪**，而
  两份 6000 token 消息列表的全文 dump 不是。逐字段比较才能点名"是 role
  变了 / 少了一条消息 / body 变了"。
- **`zip(..., strict=False)` 先按最短对齐**，长度差异留到最后单独报
  （"missing X" / "extra X"），因为长度不一致时的报错要说出**第几条开始**
  对不上，而不是笼统的 `len` 不同。
- **`sorted(set(a) | set(b))` 保证字段遍历顺序稳定**，diff 输出可复现。
- **`_window` 从第一次不一致的地方起取窗口**——第一版打印前 90 字符，在
  第一次真实 drift（系统提示词多了一句）上打印了**相同的 90 个字符**两次：
  一个展示"没变的那部分"的 diff 不是 diff（docstring 里记了这个教训）。

`_short` 和 `_window` 是给报告用的两个小工具：

```python
def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= 90 else text[:87] + "..."


def _window(was: Any, now: Any, *, before: int = 20, width: int = 70) -> tuple[str, str]:
    a = (was if isinstance(was, str) else json.dumps(was, ensure_ascii=False)).replace("\n", "\\n")
    b = (now if isinstance(now, str) else json.dumps(now, ensure_ascii=False)).replace("\n", "\\n")
    at = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
    start = max(0, at - before)
    lead = "..." if start else ""
    return (
        lead + a[start : start + width] + ("..." if start + width < len(a) else ""),
        lead + b[start : start + width] + ("..." if start + width < len(b) else ""),
    )
```

## I3 · `RecordedModel`：一个说真话的模型 double

正文 §3 讲了设计，完整实现：

```python
class RecordedModel:
    def __init__(self, recording: Recording, *, strict: bool = True) -> None:
        self.recording = recording
        self.strict = strict
        self.sent: list[list[dict[str, Any]]] = []
        self._next = 0
        self.tools: list[dict[str, Any]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        if self._next >= len(self.recording.attempts):
            raise ReplayExhausted(
                f"{self.recording.path}: the loop asked for attempt "
                f"{self._next + 1}; the recording has {len(self.recording.attempts)}"
            )
        step = self.recording.attempts[self._next]
        self._next += 1
        self.sent.append([dict(m) for m in messages])

        if self.strict:
            difference = divergence(messages, step.request)
            if difference is not None:
                raise ReplayDrift(
                    f"turn {step.turn} attempt {step.attempt}: the request is no "
                    f"longer what was recorded in {self.recording.path.name}\n"
                    f"      {difference}"
                )

        if step.failure is not None:
            raise _raise_from(step.failure)

        if step.text:
            yield TextDelta(step.text)
        for call in step.tool_calls:
            yield call
        if step.prompt_tokens is not None:
            yield Usage(prompt_tokens=step.prompt_tokens, completion_tokens=0)
        yield Completed(step.finish_reason)
```

四个要点：

1. **`self._next` 是消费指针**，`stream` 每次被调消耗一个 attempt。
   超出就 `ReplayExhausted`——"循环要的比录制多"是一个明确错误，不是
   静默停。
2. **`self.sent.append([dict(m) for m in messages])` 是浅拷贝**——存下
   "这次实际发的是什么"，供 CLI 打印 `turn N: M message(s) match`，也防止
   后续代码改动调用方的消息列表污染记录。
3. **`strict` 模式把同一份录制当快照**：每个请求都和录制的比对，第一个
   差异抛 `ReplayDrift`。docstring 里说这就是"同一个物件干两份活"——
   手写的黄金 transcript 只覆盖"记得写 fixture 的接线"，录制覆盖"上周二
   程序实际干的"。
4. **`ReplayDrift` 是 `BaseException` 不是 `Exception`**，注释里记了原因：
   第一版是 `AssertionError`，第一次真实 drift 从 CLI 冒出来时被
   `_respond` 的 `except Exception` 接住、丢给 `classify()` 当失败处理——
   测试 double 的断言掉进了被测代码的错误网里（F07-04 形状第三次）。
   **double 的断言必须比被测代码更响。**

`_raise_from` 把录制的失败重建回真实异常：

```python
def _raise_from(failure: dict[str, Any]) -> BaseException:
    status = failure.get("status")
    if status == 0:
        if failure.get("kind") == "incomplete_stream":
            return IncompleteStreamError(failure.get("detail", ""))
        return httpx.TransportError(failure.get("detail", ""))
    return ModelHTTPError(
        status=int(status or 500),
        url=failure.get("url", "replay://recorded"),
        headers=failure.get("headers") or {},
        body=failure.get("body", ""),
    )
```

`status == 0`（非 HTTP 失败）只重建两种曾到达 `classify` 的形状：流中断
和传输层错误。否则重建 `ModelHTTPError`——这样回放会走**真实的重试路径**
（429 → 退避 → 第二次尝试 → 成功），而不只是"失败然后结束"。

## I4 · `ReplayedTools` 与 `recorded_tools`：工具侧离线重建

正文 §3.4 给了 `recorded_tools` 的 docstring（"第 N+1 条请求里装着第 N 轮
的返回值"），完整实现：

```python
@dataclass
class ReplayedTools:
    outputs: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    missed: list[str] = field(default_factory=list)

    def handler(self, name: str) -> ToolFn:
        async def run(arguments: dict[str, Any]) -> str:
            key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            queue = self.outputs.get(key)
            if queue:
                return queue.pop(0)
            self.missed.append(f"{name}({_short(arguments)})")
            return f"Error: this call was not in the recording ({name})."

        return run

    def table(self, names: Iterable[str]) -> dict[str, ToolFn]:
        return {name: self.handler(name) for name in names}


def recorded_tools(recording: Recording) -> ReplayedTools:
    by_id: dict[str, str] = {}
    calls: dict[str, tuple[str, str]] = {}
    for step in recording.attempts:
        for message in step.request:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function", {})
                    arguments = function.get("arguments", "")
                    try:
                        parsed = json.loads(arguments) if arguments else {}
                    except json.JSONDecodeError:
                        parsed = {}
                    calls[call.get("id", "")] = (
                        function.get("name", ""),
                        json.dumps(parsed, sort_keys=True, ensure_ascii=False),
                    )
            elif message.get("role") == "tool":
                by_id[message.get("tool_call_id", "")] = message.get("content", "")

    replayed = ReplayedTools()
    for call_id, key in calls.items():
        if call_id in by_id:
            replayed.outputs.setdefault(key, []).append(by_id[call_id])
    return replayed
```

- **`recorded_tools` 分两遍扫**：先收集 `assistant` 消息里的调用
  （`call_id → (name, 归一化 arguments)`），再收集 `tool` 消息里的结果
  （`tool_call_id → content`），最后用 id 配对。**arguments 归一化**
  （`json.loads` 再 `json.dumps(sort_keys=True)`）——`{"path":"x.py"}`
  和 `{"path": "x.py"}` 是不同字节但同一个调用，键必须一致才能命中。
- **`ReplayedTools.outputs` 的值是 `list[str]`（队列）**：同一个调用发生
  两次（读同一个文件两遍）要回放两次。`pop(0)` 消费队列，顺序保证两次
  读的输出不会对调。
- **没有输出的调用不是错误**：返回 `"Error: this call was not in the
  recording"` 并记进 `missed`——工具抛异常会结束被测的循环（在机制内部
  失败），而返回消息是在边界上指出缺口。
- **已知边界**（docstring 里承认）：按顺序消费队列假设两次相同调用的
  执行顺序和录制顺序一致。第 8 章调度器可以把两个调用放不同批次，这个
  假设不免费——但失败是响亮的（拿到下个调用的输出，strict 模式在下一条
  请求就报 drift），所以留着。

## I5 · `evals.py`：Check 工厂、run_task、TASKS

### I5.1 Check 工厂：断言从"写句子"变成"填空"

正文 §5 给了 `Trajectory` 和 `answer_has`。完整的工厂族：

```python
def calls(name: str) -> Check:
    return Check(f"calls {name}", lambda t: t.called(name))


def never_calls(name: str) -> Check:
    return Check(f"never calls {name}", lambda t: not t.called(name))


def file_has(path: str, needle: str) -> Check:
    return Check(f"{path} contains {needle!r}", lambda t: needle in t.read(path))


def file_lacks(path: str, needle: str) -> Check:
    return Check(f"{path} does not contain {needle!r}", lambda t: needle not in t.read(path))


def file_is(path: str, content: str) -> Check:
    return Check(f"{path} is unchanged", lambda t: t.read(path) == content)


def finished() -> Check:
    return Check("ran out of neither turns nor patience", lambda t: t.stop_reason == "completed")


def asks_a_question() -> Check:
    return Check("ends by asking a question", lambda t: "?" in t.final_text)
```

- **`Check` 是 `(名字, 函数)` 对**：名字给报告打印，函数对 `Trajectory`
  提问。工厂函数把"写 lambda"包装成"填参数"——`calls("apply_patch")`
  比手写 `Check("...", lambda t: ...)` 好读，而且名字和逻辑在同一个地方。
- **全部断言在轨迹上，不在措辞上**（模块 docstring 的第一条决定）：
  `file_has` 看磁盘、`calls` 看调用记录、`file_is` 看文件没变。只有
  `answer_has` 和 `asks_a_question` 碰 `final_text`，而且 docstring 明说
  它们存在是因为"有些东西真的是关于答案的断言，诚实的做法是知道它是"。

### I5.2 `run_task`：一个任务，一个工作区，一份结果

正文 §5 讲了设计，完整实现：

```python
async def run_task(
    task: Task,
    build_model: Callable[[list[dict[str, Any]]], Model],
    *,
    workspace: Path,
    instructions: str | None = None,
    wiring: Wiring | None = None,
) -> Result:
    await asyncio.to_thread(_materialise, workspace, task.files)

    session = Session(mode=task.mode, approver=AllowAll())
    tools = local_tools(workspace, session)
    wiring = wiring or Wiring()
    agent = wiring.agent(
        build_model(tools.schemas),
        tools,
        max_turns=task.max_turns,
        instructions=instructions,
    )

    error: str | None = None
    calls_made: list[tuple[str, dict[str, Any]]] = []
    final_text, stop_reason, turns = "", "error", 0
    try:
        outcome = await agent.run(task.question)
    except Exception as exc:  # a failed run is a data point, not a crashed probe
        error = f"{type(exc).__name__}: {exc}"
    else:
        final_text, stop_reason, turns = (
            outcome.final_text,
            outcome.stop_reason,
            outcome.turns_used,
        )
        for item in outcome.history.items:
            for call in getattr(item, "tool_calls", ()) or ():
                calls_made.append((call.name, call.arguments or {}))

    trajectory = Trajectory(
        calls=tuple(calls_made),
        final_text=final_text,
        stop_reason=stop_reason,
        turns=turns,
        files=await asyncio.to_thread(_snapshot, workspace),
    )
    passed = tuple(c.name for c in task.checks if error is None and c.holds(trajectory))
    failed = tuple(c.name for c in task.checks if c.name not in passed)
    return Result(task, trajectory, passed, failed, error)
```

四个新手容易漏的点：

1. **`_materialise`/`_snapshot` 走 `to_thread`**——它们是阻塞 IO（写文件、
   `rglob`），F00-09 的 ASYNC240 规则。注释明说："linter 不知道这个程序
   一次只跑一个任务，而'有人某天并发跑任务集'的那天不该是发现它的时候。"
2. **`except Exception` 把失败变成 `Result.error`，不是让探针崩。** "一次
   失败的运行是一个数据点"——`Result.error` 有值时，所有 check 都算没过
   （`passed` 的空元组 + `failed` 列出全部），报告里显示 `TypeError: ...`
   而不是静默。
3. **`calls_made` 从 `outcome.history.items` 里收**，用 `getattr(item,
   "tool_calls", ()) or ()`——不同历史条目类型（UserMessage/AssistantMessage/
   ToolResult）只有 assistant 有 `tool_calls`，`getattr` 的默认值让循环
   对没有该属性的条目直接跳过。
4. **`passed`/`failed` 的判定顺序**：先算 `passed`（`error is None and
   c.holds(trajectory)`），再算 `failed`（`c.name not in passed`）。有 error
   时 `passed` 是空，所有 check 都进 `failed`——"运行崩了"和"check 没通过"
   在报告里都可见。

### I5.3 `Arm`、`table`、`failures`

正文 §7 给了 `regressions`（它"是这整个模块存在的理由"），`Arm` 和
`table` 没进正文：

```python
@dataclass
class Arm:
    name: str
    results: list[Result] = field(default_factory=list)

    def rate(self, task: str) -> tuple[int, int]:
        rows = [r for r in self.results if r.task.name == task]
        return sum(1 for r in rows if r.ok), len(rows)

    @property
    def total(self) -> tuple[int, int]:
        return sum(1 for r in self.results if r.ok), len(self.results)


def table(arms: Sequence[Arm], tasks: Sequence[Task]) -> str:
    width = max(len(t.name) for t in tasks) + 2
    lines = ["  " + "task".ljust(width) + "  ".join(a.name.rjust(12) for a in arms)]
    for task in tasks:
        cells = []
        for arm in arms:
            ok, total = arm.rate(task.name)
            cells.append(f"{ok}/{total}".rjust(12))
        lines.append("  " + task.name.ljust(width) + "  ".join(cells))
    totals = []
    for arm in arms:
        ok, total = arm.total
        totals.append(f"{ok}/{total}".rjust(12))
    lines.append("  " + "TOTAL".ljust(width) + "  ".join(totals))
    return "\n".join(lines)
```

- **`rate` 返回 `(ok, total)` 元组而不是百分比**——`sum(...)`, `len(...)`
  是精确计数，`6/8` 比 `75%` 保留分母信息（8 次里 6 次 vs 4 次里 3 次
  不是一回事）。
- **`table` 永远按任务 × 臂打印，不只打印总数**（docstring 明说：
  "总数是隐藏值得知道的东西的那个数字——两个臂都是 18/24 可能在每个任务
  上都不一样"，F14-07）。`ljust`/`rjust` 做对齐，`TOTAL` 行收尾。
- **`failures` 列出每个失败结果的详情 + 轨迹**：

```python
def failures(arm: Arm) -> list[str]:
    lines = []
    for result in arm.results:
        if result.ok:
            continue
        detail = result.error or ", ".join(result.failed)
        lines.append(f"  {result.task.name}: {detail}\n      {result.trajectory.describe()}")
    return lines
```

`result.error or ", ".join(result.failed)`：运行崩了显示异常，没崩显示
没过的 check 名列表；`trajectory.describe()`（`" -> ".join(call names)`）
让人一眼看到失败时模型做了什么。

### I5.4 `TASKS` 与三个辅助函数

六个任务的完整定义在源码里（`CALC`/`TEST_CALC`/`BROKEN`/`TEST_BROKEN` +
`TASKS` 元组，正文 §7.1 只展示了形状）。三个辅助函数没进正文：

```python
def by_name(name: str) -> Task:
    for task in TASKS:
        if task.name == name:
            return task
    raise KeyError(name)


def declared_files() -> set[str]:
    return {name for task in TASKS for name in task.files}


def as_json() -> str:
    return json.dumps(
        [
            {
                "name": t.name,
                "question": t.question,
                "files": sorted(t.files),
                "checks": [c.name for c in t.checks],
            }
            for t in TASKS
        ],
        indent=2,
        ensure_ascii=False,
    )
```

- **`by_name` 是 `for ... raise KeyError` 的经典模式**——找不到就是
  `KeyError`，调用方（探针的 `--only`）能按任务名选中一个。
- **`declared_files` 服务 F14-08 的断言**：任务集声明的工作区文件集合，
  测试用它确认"评估工作区里没有藏着答案或测试"。
- **`as_json` 把任务集变成数据**：`files` 只列路径（不列内容）、`checks`
  只列名字——"某个 diff 改了任务集"时，这份 JSON 是能读的差异。

## I6 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| 回放把 429 变成空白回答 | 录制格式扁平化时 `kind` 字段被覆盖 | `outcomes` 用两个 dict 存 `(kind, payload)`，不合并 |
| 压缩事件让 request/response 配不上 | 按相邻行配对 | 按 `(turn, attempt)` 键配对，`compaction` 行直接跳过 |
| 旧录制回放时悄悄填 `{}` | `load`/`_calls` 缺字段检查 | 拒绝并点名缺什么字段 |
| drift 被 `_respond` 当失败重试 5 次 | `ReplayDrift` 是 `Exception` 子类 | 改成 `BaseException`，比被测代码的 `except Exception` 更响 |
| 两次相同调用输出对调 | 输出按 `(name, args)` 单值存 | `outputs` 值是 `list`，`pop(0)` 消费队列 |
| 工具调用记录没参数，回放不出来 | 录制格式只有 id/name | `call_record` 存 `raw_arguments`；旧录制拒绝 |
| 失败只存了分类没存证据 | `failure_record` 只记 `Failure` | 存 status/url/headers/body，`status: 0` 表示非 HTTP |
| `_window` 打印相同的 90 字符两次 | 从头打印 | 从第一次不一致处起取窗口 |
| 运行崩了但 check 全绿 | `except` 没把错误放进 `Result` | `Result.error`，有 error 时 `passed` 为空、全部进 `failed` |
| 评估工作区里有答案/测试 | `Task.files` 没隔离 | `_materialise` 从 `files` 映射重建，`declared_files()` 断言隔离 |
| 响应事件配到错误的 attempt | `response` 没 attempt 字段 | 取该 turn 已有 request 的最大 attempt |
