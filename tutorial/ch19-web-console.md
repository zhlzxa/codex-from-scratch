# 第 19 章 · Web 控制台：换一个人机界面，Agent 一行都不用改

> **代码**：`steps/step19_web_console/`
> **分支**：`feat/web-console`
> **产出**：`src/minicodex/web/`（8 个模块，1898 行）、`minicodex serve` 子命令、
> `frontend/`（React + Vite，2216 行）、`scripts/check_layers.py` 的两处修复、
> `probe_web.py` 四个测量段
> **你需要**：本章 40 个测试全部离线，`probe_mutations_ch19.py` 离线，
> `probe_web.py` 的 `core-diff` / `order` / `bundle` 三段离线。
> 只有 `probe_web.py stream` 要真调 API（openai，几美分）。
> **本章新增一条环境要求**：前端要 Node ≥ 20。后端和 1721 个测试都不要。
>
> **阅读顺序在第 15 章之前，编号在其之后。** 理由和 Ch16/Ch17/Ch18 一样。

---

## §1 这一章要做出来的东西

### 1.1 先看见它动

```
$ cd steps/step19_web_console
$ uv sync --all-extras
$ npm --prefix frontend ci && npm --prefix frontend run build
$ uv run minicodex serve
INFO     Started server process [24188]
INFO     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

打开 `http://127.0.0.1:8000`：左边是按**工作目录分组**的会话列表，中间是对话流，
底部输入框右侧是当前模型，会话标题旁边常驻两个 chip——一个写着
`read-only · on-request`，一个写着 `live`。

发一句"把 README 里的拼写错误改掉"，然后你会看到这些东西依次出现：

- 一个 `shell` 工具块，参数摘要在标题行，输出在下面的 `<pre>` 里；
- 一张淡紫色的审批卡片，左边一道 3px 竖线，中间是**可编辑的命令输入框**，
  下面三个按钮：`Approve once` / `Always in this project` / `Deny`；
- 你点 Approve，卡片变灰，工具块的 `running…` 被真实输出替换；
- 最后一行小字：`[gemma4:31b-cloud | done after 3 turn(s)]`。

**那张审批卡片不是模拟的。** 你点下去之前，服务器上那个 `agent.run()` 协程真的
停在 `await` 上——停在 `minicodex/approval.py` 第 5 章写的那个 `Approver` 协议里，
和 CLI 停在 `input()` 上是同一个位置、同一个原因。

### 1.2 这一章要回答的那个问题

这个项目已经有 18 章了。`agent.py` 里那个循环、`tools.py` 里那些工具、
`rollout.py` 里那套落盘和恢复——全都是为一个**终端**写的：`announce=print`，
`CliApprover` 调 `input()`，`--resume last` 从命令行参数进来。

现在要换一个完全不同的人机界面。问题是：

> **要改多少 Agent 的代码？**

这个问题值得测，不值得猜。因为它是那种"你以为答案是零，写完发现是三百行"的问题，
也是那种"写完之后慢慢就不是零了"的问题。所以本章有一个脚本专门算这个数，
每次跑都重算：

```
$ uv run python probe_web.py core-diff
== core-diff ==

baseline: steps\step18_skills\src\minicodex

    30 top-level modules byte-identical to chapter 18
     4 changed only by chapter 15's fixes: agent.py, registry.py, replay.py, subagent.py
     0 agent modules changed for the console  <- the headline

  __main__.py: +57 lines, all of it the `serve` subcommand
  new backend  (8 modules): 1898 lines
  new frontend (9 files):   2216 lines
```

**零。** 30 个顶层模块和第 18 章逐字节相同。唯一动过的是 `__main__.py`，
多了 57 行，全部是 `serve` 这个子命令的参数和分发——那是**入口**学会了一件新事，
不是 Agent 变了。

那 4 个"只被第 15 章的修复改过"的文件下面 §11 会解释。

先说清楚这个零是怎么来的，因为它不是运气。

---

## §2 三条接缝，都是别人为别的理由留的

浏览器需要三件 CLI 不需要的东西：**把审批问出去、把过程播出去、把对话接回来**。
这三件事各自需要一个口子。三个口子都已经在了。

### 2.1 审批：`Approver` 是个 Protocol（第 5 章）

```python
# approval.py，第 5 章写的
class Approver(Protocol):
    async def ask(self, request: ApprovalRequest) -> ApprovalReply: ...
```

第 5 章把它写成 Protocol，理由和浏览器没有一点关系：当时是为了让测试能塞一个
`AllowAll()` 进去，不用真的读 stdin。但一个只有一个 `async` 方法的协议，
换成"挂在一个 `asyncio.Future` 上，等一个后来的 HTTP 请求来解开它"是天然成立的：

```python
# web/approver.py，本章新增的全部内容之一
class WebApprover:
    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        approval_id = uuid.uuid4().hex[:12]
        future = self._broker.register(approval_id)
        self._emit({"type": "approval_request", "id": approval_id, ...})
        reply = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS)
        return reply
```

`agent.py` 一行没动。`approval.py` 一行没动。`policy.py` 一行没动。

### 2.2 过程：`RolloutWriter.append` 就是 `History` 的观察者钩子（第 7 章）

第 7 章为了断电之后还能接上，让 `History` 每接受一条 item 就回调一次
`observer`，`RolloutWriter` 就挂在这个回调上。

于是"把过程播给浏览器"这件事有一个白送的实现——**继承那个 writer**：

```python
# web/events.py，本章新增的全部内容
class StreamingRolloutWriter(RolloutWriter):
    def append(self, item) -> None:
        super().append(item)
        self._emit({"type": "history_item", "item": _dump_item(item)})
```

这 4 行买到的东西比看上去多。它保证了一条性质：

> **一个事件到达浏览器，当且仅当它也已经 fsync 到磁盘。**

因为只有一个调用点，而那个调用点两件事都做。你不可能看到一条浏览器上有、
文件里没有的消息——这是"直播"和"落盘"两套代码永远做不到的。

### 2.3 续聊：`resolve("last", dir)` 已经是"接着上次"（第 7 章）

CLI 是 `minicodex ask ... --resume last`。控制台里一个 thread 就是一个目录，
续聊就是在**这个 thread 自己的目录**里 `resolve("last", ...)`：

```python
previous_path = resolve("last", thread_dir)
```

作用域从"全局最后一次"缩到"这个目录里最后一次"，别的什么都没变。
这也是为什么两个浏览器标签页永远不会串台——**不是因为写了隔离逻辑，
是因为它们查的根本是两个目录。**

### 2.4 组装：`composition.py` 只有一个（插曲 B）

插曲 B 把"一次运行怎么装起来"收进了 `composition.py`，理由是当时
`__main__` 和 `subagent.py` 各装各的，装出来的东西不一样，测量到的差别是
181,040 字符 vs 76,896 字符。

那次重构的直接结果是：本章的 `web/runtime.py` 只要调**同一组函数**，
就能得到和 `minicodex ask` 一模一样的一次运行。它调的是：

```python
tools = top_level_tools(root, session, sub_ctx, plan=..., memory=..., remember=..., skills=...)
tools = watching(with_remote_tools(tools, registry), task_plan)
agent = wiring.agent(llm, tools, instructions=..., rollout=writer, on_turn_start=..., ...)
```

一行都不是新写的。**如果插曲 B 没做，这一章要么改 `__main__`，
要么复制一份装配逻辑然后慢慢和它漂移。**

---

## §3 F19-01：两条消息一起到，起了两个 turn

第一版的发消息路由长这样：

```python
@app.post("/api/threads/{thread_id}/messages", status_code=202)
async def send_message(thread_id: str, body: SendMessage):
    if thread_id in _busy:                       # ← 检查
        raise HTTPException(409, "still answering")

    async def go() -> None:
        _busy.add(thread_id)                     # ← 占位
        try:
            await run_turn(...)
        finally:
            _busy.discard(thread_id)

    asyncio.create_task(go())
    return {"accepted": True}
```

看起来对。**它不对。**

`asyncio.create_task(go())` 只是**排期**，`go()` 的第一行要等到当前这个协程
让出控制权之后才会跑——而当前这个协程接下来做的事是 `return`，也就是把 202
发回浏览器。所以在"检查"和"占位"之间隔着一整个 HTTP 响应。

两条 POST 挨着到，**两条都能过那个检查**。然后同一个 thread 目录上开了两个
`StreamingRolloutWriter`，两个都在写，`resolve("last", ...)` 下一轮拿到哪个
看谁先落盘。

修法一行：

```python
    if thread_id in console.busy:
        raise HTTPException(409, "this thread is still answering the previous message")
    console.busy.add(thread_id)          # ← 同步，在 create_task 之前
```

### 3.1 这条的测试比修法难写

写完测试第一次跑，**三条全绿——但它们在骗人**。

```python
first = console.post(f"/api/threads/{thread_id}/messages", json={"text": "one"})
second = console.post(f"/api/threads/{thread_id}/messages", json={"text": "two"})
assert second.status_code == 409
```

问题是 `TestClient` 每个请求跑完就把事件循环收了，那个 `run_turn` 早就结束
（连不上 provider，秒失败），第二条 POST 到的时候 `busy` 已经空了。
**这个测试对着修好的代码和坏掉的代码都是同样的结果**——绿，但绿的原因不对。

两处改动才让它真的测到东西：

```python
@pytest.fixture
def console(tmp_path: Path) -> Any:
    # 用上下文管理器：让 lifespan 跑起来，让一个事件循环活过多个请求
    with TestClient(create_app(tmp_path / "console")) as client:
        yield client


@pytest.fixture
def hanging_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 run_turn 换成一个永远不结束的。"""
    async def never(**_kwargs: Any) -> None:
        await asyncio.sleep(3600)
    monkeypatch.setattr(routes, "run_turn", never)
```

这是第 14 章那条规矩的又一次应用：**一个对着错误实现也通过的测试，
什么都没测**。第 18 章的 `read_skill` 路径穿越测试栽在同一个坑里
（那次是"重建出来的路径刚好不存在"），这次是"那一轮刚好已经结束了"。

变异脚本确认了这次是真的：

```
    4 test(s) fail  <-  the busy flag is claimed inside the task again, not before it
```

> **写在后面。** 这一节的口气配不上这一章的实际情况。
> 本章 README 上写着 **32 mutations, 0 survivors**，而真跑起来有 **3 条活着**，
> 并且活着的正是这一章自己在"新增了什么"表格里吹的两个机制——
> `channel.py` 的"顺序由构造保证，不靠运气"和 `approver.py` 的 Future 挂起。
> 三条对应的测试都存在、都是绿的，把机制删掉照样绿：
>
> - 顺序测试给每次发送定了**同样的耗时**，于是一百个扇出任务按创建顺序恢复，
>   断言成立靠的是调度器的 FIFO 习惯，不是那个泵；
> - "第二次应答"测试依赖 `resolve` 会 `pop`，所以永远在 `or` 的前半截就返回了，
>   真正要守的后半截（`wait_for` 超时取消了 future、`discard` 还没跑，
>   这时回复到达，`set_result` 在路由里抛 `InvalidStateError`）一次都没走到；
> - `call_soon_threadsafe` 那一跳**压根没有测试**——因为数据竞争在测试里
>   每一次都会给出正确答案。
>
> 三条现在都补上了（`test_F19_03_order_holds_when_the_early_events_are_the_slow_ones`、
> `test_F19_02_a_reply_that_lost_the_race_with_the_timeout_is_refused`、
> `test_F19_02_a_reply_from_a_worker_thread_goes_through_the_loop`）。
> 最后一条断言的是**机制**而不是结果——`future.set_result in scheduled`——
> 因为结果两种写法下都是对的，能观测的只有"这一跳到底交给了事件循环没有"。
>
> 值得记下来的不是"要写更好的测试"。是这一章一边写着
> "一个绿勾一直在说没问题"来说 `check_layers.py`，一边自己就有三个，
> 而且**那句 0 survivors 是写上去的，不是量出来的**——
> 一份通过的测试集和一句验证声明是两件事，这次只有前者被真的执行过。

---

## §4 F19-02：审批是在另一个线程里解开的

`WebApprover.ask` 挂在一个 `asyncio.Future` 上，浏览器点了按钮之后由这个路由解开：

```python
@router.post("/api/approvals/{approval_id}/respond")
def respond_to_approval(approval_id: str, body: ApprovalResponse):     # ← def
    ...
    broker.resolve(approval_id, reply)     # 里面是 future.set_result(reply)
```

FastAPI 有一条规矩：**声明成 `def` 的路由会被丢进 anyio 的线程池里跑**，
这样一个阻塞的同步函数才不会卡住整个事件循环。这是个好设计。

但 `asyncio.Future` **不是线程安全的**。从一个不是事件循环那个线程的地方调
`set_result`，你做了两件事：改一个没有锁保护的状态，以及**指望事件循环自己发现
这个 future 已经好了**。后者是不会发生的——唤醒等待者的回调是通过
`loop.call_soon` 排进去的，而 `call_soon` 本身也不是线程安全的。

失败长什么样：一次审批点了，浏览器上卡片变灰了，**然后那一轮再也不动**，
直到 600 秒的审批超时把它当成拒绝。你去看日志——没有异常，没有堆栈，
什么都没有。它看起来就像模型在想事情。

而且它**大部分时候是对的**。所以这不是一个你能复现的 bug。

两道防线，因为其中一道是约定，而约定会走丢：

```python
# routes.py —— async def，所以它就跑在事件循环上
async def respond_to_approval(...) -> dict[str, bool]:

# approver.py —— 不管谁调，都走 call_soon_threadsafe
    def resolve(self, approval_id: str, reply: ApprovalReply) -> bool:
        ...
        if loop is None or loop is _running_loop():
            future.set_result(reply)
        else:
            loop.call_soon_threadsafe(future.set_result, reply)
```

测试从线程池里解开它，验证等待方真的被唤醒：

```python
    done = await asyncio.get_running_loop().run_in_executor(
        None, lambda: broker.resolve("abc", reply)
    )
    assert done is True
    assert await asyncio.wait_for(future, 2.0) == reply
```

顺手还有一条：`asyncio.get_event_loop()` 从 3.10 起就废弃了，
没有运行中的循环时它会**新建一个**——那样 future 会属于一个永远不会跑的循环。
本章统一用 `get_running_loop()`。

---

## §5 F19-03：事件顺序不是坏，是"0% 然后 100%"

把事件发给所有订阅者，最自然的写法是这样：

```python
def send(event: dict[str, Any]) -> None:
    payload = json.dumps(event)
    for ws in list(_subscribers.get(thread_id, ())):
        asyncio.create_task(_safe_send(ws, thread_id, payload))
```

每个事件、每个 socket，一个独立的 task。**每个 task 在写第一个字节的时候会挂起**，
挂起之后循环按什么顺序恢复它们，没有任何保证。

所以浏览器上可能出现：一条 `history_item` 排在它后面才发出的 `turn_complete`
**之后**。不抛异常，不丢事件，日志干干净净——只是那份 transcript 偶尔是乱的。

### 5.1 先量，再修

"偶尔"是多偶尔？这个数字决定了这条 bug 会不会被用出来。所以先写了
`probe_web.py order`：把坏版本和好版本各喂 200 个事件，数**逆序对**
（一个事件排在了比它晚发出的事件后面）。

```
$ uv run python probe_web.py order
== order ==

20 trials x 200 events, one subscriber

      even sends: naive     0 inversions (0/20 runs affected)   queue 0 inversions
    uneven sends: naive    20 inversions (20/20 runs affected)   queue 0 inversions
```

**结果不是"有时候"，是 0% 和 100%。**

- 每个 send 都正好花一轮循环（`await asyncio.sleep(0)`）：坏版本
  **20 次全对**，一个逆序都没有。因为 task 是按顺序建的，每个都在下一个恢复
  之前跑完了。
- 三个 send 里有一个花两轮（写缓冲偶尔满了就是这样）：**20 次全错**。

这个形状比"4% 的概率"糟糕得多。它意味着：

> **在你的笔记本上、一个标签页、一个本地模型，它是完全正确的。
> 换成真网络、两个标签页、一个文件那么大的工具输出，它每次都错。**

这是一条**你不可能靠用它发现的 bug**。

### 5.2 修法：一个队列，一个泵

```python
# web/channel.py
async def _pump(self, thread_id: str, queue: asyncio.Queue[str | None]) -> None:
    while True:
        payload = await queue.get()
        if payload is None:
            return
        for socket in list(self._sockets.get(thread_id, ())):
            try:
                await socket.send_text(payload)     # ← 一个一个 await
            except Exception:
                self._sockets.get(thread_id, set()).discard(socket)
```

顺序来自队列，不来自运气。代价是慢一点——一个人开着的控制台，socket 数量
根本不够让这个差别可测量，而"浏览器上的对话顺序是乱的"是一个人在浏览器里
永远调不出来的东西。

`emit` 保持**同步**，这一点是硬约束：它是从 `History` 的观察者里调的，
在 `agent.run` 底下三层，那个地方不能 `await`。`put_nowait` 是唯一装得进去
的形状，也是"核心不需要知道 WebSocket 是什么"的原因。

---

## §6 F19-04：订阅表只增不减

`unsubscribe` 第一版：

```python
_subscribers.get(thread_id, set()).discard(ws)
```

socket 拿掉了，**那个空 set 留下了**。每开过一个 thread，字典就永久多一项。
笔记本上看不出来，一个跑着不关的服务上就是一条慢泄漏。

同一个地方还有第二个形状相反的问题：`asyncio.create_task` 返回的 task
**调用方必须自己拿住**——事件循环只持有弱引用，CPython 有权在一个 task
`await` 到一半的时候把它回收掉。文档里写着，很少有人照做。

修法是把三样东西绑成一组生死：队列、socket 集合、泵任务，一起建，一起扔。
`_pumps` 这个字典本身就是那个强引用。

```python
    def unsubscribe(self, thread_id: str, socket: Socket) -> None:
        ...
        self._sockets.pop(thread_id, None)      # 连空集合一起扔
        queue = self._queues.get(thread_id)
        if queue is not None:
            queue.put_nowait(None)              # 哨兵，不是 cancel
```

用哨兵而不是 `task.cancel()`：已经排队的事件还要发给剩下的人，
取消会把它们连同队列一起丢掉。

---

## §7 F19-05：掉线之后，那个页面就再也不动了

旧控制台的 socket 是这么连的：

```javascript
function connectSocket(threadId){
  var socket = new WebSocket(scheme + "//" + location.host + "/ws/threads/" + threadId);
  socket.onmessage = function(evt){ ... };
  ws = socket;
}
```

只有 `onmessage`。没有 `onclose`，没有 `onerror`，没有重连。

服务器 `--reload` 一次、笔记本睡一次、网络抖一下——socket 悄悄死掉，
页面停在 `thinking…`，而**那一轮在服务器上还在跑**。唯一的恢复手段是 F5。

最刺眼的是同一份代码里还有这一行：

```javascript
if (ws){ ws.onclose = null; ws.close(); ws = null; }
```

`ws.onclose = null`——**清掉一个从来没被赋过值的处理器**。重连显然是想过的，
只是没写。这种"半截痕迹"比完全没想过更值得记一笔。

### 7.1 光重连不够

新版本用退避重连。但重连本身解决不了问题，因为后端对**没人在看的 thread
是直接丢事件的**（见 §6 的设计：不丢就是内存泄漏）。所以掉线那段时间发生的事，
重连回来是补不回来的。

补回来的办法是：**每次重连成功之后，从 `GET /api/threads/{id}` 重新拉一次**。

```typescript
socket.onopen = () => {
  setConnection("open");
  const wasRetry = retriesRef.current > 0;
  retriesRef.current = 0;
  if (wasRetry) void resync(threadId).catch(() => undefined);
};
```

而那个接口读的，是**事件本来就是从里面派生出来的那个 rollout 文件**。

这就是 §2.2 那条性质的回报：因为"发给浏览器"和"写到磁盘"是同一个调用点，
socket 才可以是一个**优化**而不是唯一的真相来源。丢了它是可以恢复的。
如果当初写了两套路径，这里就得发明一个重放缓冲区。

顺序也是有讲究的：先订阅再拉，不能反过来——反过来会在"拉完"和"订上"之间留一条缝，
缝里发生的事件谁也不知道。

---

## §8 F19-06 / F19-07：检查器早就不看新代码了，而且一直是绿的

这一章最贵的两条故障不在控制台里。

### 8.1 一个子包，五条规则，零覆盖

`scripts/check_layers.py` 是插曲 B 写的，五条规则：不许有循环、`__init__`
必须是叶子、五条禁止边、只能有一个 `Agent(...)` 构造点、每个模块能单独 import。
它在 lint 步骤里跑，每个 PR 都跑。

它这样找模块：

```python
    for path in sorted(pkg.glob("*.py")):
```

`glob`，一层。这在十八章里都是对的，因为这个包一直是平的。

然后这一章加了 `src/minicodex/web/`，八个模块。

**五条规则，一条都没看到它们。** 没有报错，没有警告——检查器在 lint 步骤里
对着它从来没读过的代码，报告成功。

这是一个检查能有的最坏形状，值得把话说明白：

> **一条会悄悄停止适用于新代码的规则，比没有这条规则更危险。**
> 因为那个绿勾在承诺已经不成立之后，还在做同样的承诺。

插曲 B 的论点是"把规则写在会执行它的地方"。这一条是它的续集：
**执行它的那个东西，得能看见代码。**

修法是 `rglob`，加上从相对路径推出点分模块名：

```python
def iter_modules(pkg: Path) -> list[tuple[str, str, Path]]:
    out: list[tuple[str, str, Path]] = []
    for path in sorted(pkg.rglob("*.py")):
        parts = list(path.relative_to(pkg).with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
            package = ".".join([PACKAGE, *parts])
        else:
            package = ".".join([PACKAGE, *parts[:-1]])
        name = ".".join([PACKAGE, *parts]) if parts else PACKAGE
        out.append((name, package, path))
    return out
```

### 8.2 修完之后，那八个模块之间一条边都没有

改成 `rglob` 之后再跑，检查器看见了八个模块——**互相之间零依赖**。
`web/app.py` 明明 import 了 `web/routes.py`。

因为 `intra_package_imports` 是这么写的：

```python
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
```

`node.level == 0` 意思是"只看绝对导入"。相对导入直接跳过。
这在包是平的时候永远无所谓——**而一个子包是按子包的写法写的，
从头到尾全是 `from .foo import x`**。

两个洞，一个症状：代码在那儿，规则没在看。第二个洞只有在第一个洞补上之后
才看得见。

```python
            if node.level:
                # `from .foo import x` inside minicodex.web -> minicodex.web.foo
                base = package.split(".")
                del base[len(base) - node.level + 1 :]
                anchor = ".".join(base)
                ...
```

### 8.3 修好的第一分钟，它就抓到了 F19-08

补完两个洞跑第三次：

```
$ uv run python scripts/check_layers.py
no import cycles: import cycle: minicodex.__main__ -> minicodex.web -> minicodex.web.app
                                -> minicodex.web.routes -> minicodex.web.store
forbidden edges: __main__ must not import ['minicodex.web'] (F19-08: ...)
```

`web/store.py` 里有这么一段，为了给第一次启动种两个 provider：

```python
        from minicodex.__main__ import PROVIDERS       # ← 向上够了
```

`__main__` 是这一层**上面**那一层。一个下层模块伸手去上层拿常量，
就是第 1 章那条循环（F01-08）换了个名字重新长出来。它 import 得动，
因为 Python 容忍在调用时才解开的循环——所以它不会自己报错。

**F19-06 和 F19-07 在修好的第一分钟就把成本赚回来了。**

修法是第 3 章的老办法。base URL 从 `model.py` 拿（provider 的事实本来就住在那儿），
两个模型名在 `store.py` 声明，然后写一个测试盯着它们和 `__main__.PROVIDERS` 一致：

```python
def test_F19_08_the_seeded_providers_agree_with_the_cli() -> None:
    from minicodex.__main__ import PROVIDERS
    seeded = {p["provider"]: (p["base_url"], p["model"]) for p in SEED_PROVIDERS}
    assert seeded == {kind: (url, model) for kind, (url, model) in PROVIDERS.items()}
```

> 两个地方必须说同一件事，而且谁都不能 import 谁——那么让它们一致的最便宜的
> 办法，是写一个比较它们的测试。第 3 章为工具描述做过一次，这里是第二次。

再加一条禁止边，把规则写在会执行它的地方：

```python
    (
        "*",
        "web",
        "F19-08: the console is the outermost layer, beside __main__. Nothing "
        "below it may import it, or the agent grows a dependency on a browser",
    ),
```

`__main__` 是豁免的，而且豁免写成一个**你读得懂的名单**而不是一个通配符：

```python
TOP_LEVEL_ENTRY_POINTS = {"__main__"}
```

`__main__` 不在控制台**下面**，它在它**旁边**：两个都是入口，而 CLI 正是
启动控制台的那个东西（`minicodex serve`）。真正要挡的是 `agent`、`tools`、
`composition`、`rollout` 这些往上够。

---

## §9 F19-09：压缩发生了，屏幕上什么都没有

`agent.py` 会往 rollout 里写三种 mark：

| mark | 什么时候 | 意味着什么 |
|---|---|---|
| `compacted` | 上下文超了，压缩跑了 | **屏幕上这段对话，已经不是发给模型的那段了** |
| `budget_exhausted` | 轮数用完 | 它停下来不是因为做完了，是因为没轮次了 |
| `interrupted` | 被打断 | 下一条消息会从这里接上 |

`StreamingRolloutWriter` 把它们都发出去了。前端的事件分发里**根本没有 `mark`
这个分支**——整类丢弃。

于是一次运行可以把一半历史压掉，屏幕上一个字都不说。

这三条是"你正在读的对话"和"模型正在收到的对话"之间唯一的告示牌。
补上三个分支，并且写一个测试盯着 `agent.py`：

```python
def test_F19_09_the_frontend_renders_every_mark_the_agent_writes() -> None:
    source = (FRONTEND / "src" / "components" / "Chat.tsx").read_text(encoding="utf-8")
    agent = (SRC / "agent.py").read_text(encoding="utf-8")
    for mark in ("compacted", "budget_exhausted", "interrupted"):
        assert f'"{mark}"' in agent, f"agent.py no longer writes {mark}; update this test"
        assert mark in source, f"the console drops the {mark} mark"
```

两个方向都断言：控制台漏了一个会红，`agent.py` 改了名字也会红——
后者的报错信息直接告诉你去改测试，而不是让你困惑。

---

## §10 F19-10 / F19-11：路径和默认值

### 10.1 打错一个字，就在任意位置建了个目录

`POST /api/threads` 收什么字符串都认，然后 `run_turn` 里：

```python
    root.mkdir(parents=True, exist_ok=True)
```

这行对 Agent 是**对的**——它得能在一个正在被创建出来的目录里干活。
对一个人在浏览器里手打的字符串是**错的**：打错一个字，服务器就在它有写权限的
任何地方建出一整棵目录树，然后在里面跑一个 Agent。

修法不是加 try，是**把校验放到字符串进入程序的地方**：

```python
def _validated_workspace(raw: str) -> str:
    ...
    if not path.is_absolute():
        raise HTTPException(400, "workspace must be an absolute path")
    if not path.exists():
        raise HTTPException(400, f"no such directory: {path}")
    if not path.is_dir():
        raise HTTPException(400, f"not a directory: {path}")
    return str(path.resolve())
```

然后 `run_turn` 里那两行（`resolve()` 和 `mkdir`）就可以删掉——调用方保证了。

同一个路由还收沙箱模式。**一个瞎编的沙箱模式也不会失败**：`Session.mode`
是个运行时没人检查的 `Literal`，一个不认识的值会从 `judge_command` 的每个
分支里漏下去，落到那个分支各自的默认值上。所以它不是"被拒绝"，
它是**不可预测**——具体落到哪儿取决于走到哪个分支。

```python
def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    merged = {**DEFAULT_THREAD_SETTINGS, **settings}
    if merged["sandbox_mode"] not in SANDBOX_MODES:
        raise ValueError(f"unknown sandbox mode {merged['sandbox_mode']!r}")
    ...
```

而前端能选的那几个值，是从 `GET /api/policy` 拿的，那个接口直接读
`policy.SANDBOX_MODES`——**界面上不可能出现核心没实现的模式，
核心新增一个模式也不会在界面上消失。**

### 10.2 一个默认值不是一次同意

第 17 章的 F17-11 说的是：memory 打开会改变此后**每一轮**发出去的东西，
所以它必须是显式开启的。

放到浏览器里，这条只会更强：

- 读 memory：重写每一个请求；
- 写 memory：在每一轮旁边起第二个模型调用，花同一份额度，
  读的是**整段 transcript**——包括 Agent 打开过的每个文件的内容。

所以：

```python
DEFAULT_THREAD_SETTINGS: dict[str, Any] = {
    "sandbox_mode": "read-only",
    "approval_policy": "on-request",
    "context_window": None,
    "memory": False,
    "remember": False,
    "skills": False,
}
```

三件事跟着这个默认值一起做：

1. **读和写是两个开关**，不是一个。`--remember` 不带 `--memory` 是一个真实
   配置（只写不读），界面必须能表达它，否则两次同意被偷偷合并成了一次。
2. **写要再确认一次**——一个对话框说清楚它会读什么、花什么、以及能怎么清掉。
   codex 的 TUI 把 `Enable memories?` 做成确认弹窗，是同一个理由。
3. **清空按钮和开关放在同一个面板里**。codex 的 `Reset all memories?` 就在
   `Enable memories?` 旁边。一个会积累你信息的功能，必须在你打开它的地方
   提供结束它的办法。

---

## §11 那四个"只被第 15 章改过"的文件

`core-diff` 报告里有这么一行：

```
     4 changed only by chapter 15's fixes: agent.py, registry.py, replay.py, subagent.py
```

这一章的阅读顺序在第 15 章**之前**，所以它本来不该有第 15 章的修复。
但其中一条是这样的（F15-02）：

```python
    except TimeoutError:            # ← subagent.py，第 10 章写的
```

`asyncio.wait_for` 抛的是 `asyncio.TimeoutError`。在 3.11+ 这两个是同一个对象；
在 **3.10——也就是 `requires-python` 承诺的地板——它们是毫无关系的两个类**。
所以子 Agent 的超时防御在这个项目承诺支持的最老解释器上，**完全没生效**。

选择是：为了让 `core-diff` 的数字好看，发一个带已知 bug 的控制台，
还是把这四处修复搬过来、然后在报告里如实分成两栏。

搬过来，分两栏。**一个为了让指标好看而保留的 bug，是指标在指挥工作。**

（本章自己写的每一处 `wait_for` 都用 `asyncio.TimeoutError`。
`__main__._finish_writer` 里还留着一处旧写法，记在未编号表里——
改它属于第 15 章。）

---

## §12 那个没修的东西：逐 item 流式要多等 2.2 秒

控制台是**按 history item** 流式的，不是按 token。这是 §2.2 那个白送的实现
带来的：复用观察者钩子，落盘即推送，一行核心代码都不用改。

代价是：模型开始说话之后，你要等它**这一条说完**才看得见。

这个代价是多少？没人知道，所以量了一次。同一个请求里取两个时间点——
第一个 `TextDelta`（token 流式能画出来的时刻），和 `Completed`
（`History` 接受这条 item、观察者触发、控制台发事件的时刻）：

```
$ uv run python probe_web.py stream
== stream ==

  12 requests, gpt-4o-mini

    first token visible :   1.97s
    item complete       :   4.20s
    the reader waits    :   2.23s longer on average
    best / worst        :   0.47s /   3.64s
```

**平均多等 2.23 秒，最坏 3.64 秒。**

这不是一个可以糊弄过去的数字，本章也不打算糊弄。诚实的结论是三句话：

1. **工具密集的一轮基本不受影响。** 每个工具调用、每条工具结果都是一条独立
   的 item，本来就是一块一块出来的。
2. **长篇散文回答受影响最大**，而且代价随模型说得多而涨——所以要看的是
   最坏那个数，不是平均数。
3. **修它需要写第二条从 model client 出来的流式路径**，那条路径有它自己的
   顺序问题和自己的失败模式，而且**它会是这一章第一次真的去改 Agent**。

所以它被写下来了，没有被做。这个数字就是将来做它的理由。

### 12.1 第一次测量是错的

第一版 `_measure` 分两次请求测两条臂，跑出来是：

```
    the reader waits    :  -0.19s longer
```

**负的。** "整条消息说完"比"第一个字出来"还早，这不是一个慢结果，
这是一个不可能的结果。原因是两次请求之间的抖动比两条臂的差还大。

改成**一次请求里取两个时间点**，这个问题从根上消失了——同一条流上的两个
时刻不可能对"谁先谁后"有分歧。

> 第 14 章的话再说一遍：一个测出不可能结果的实验，是在告诉你实验错了，
> 不是在告诉你世界错了。

---

## §13 清点：这一章写了多少代码

后端，`src/minicodex/web/`：

| 文件 | 行数 | 完整代码在哪一节 |
|---|---|---|
| `routes.py` | 609 | §3（占位）、§10.1（路径校验）、附录 I3 |
| `runtime.py` | 485 | §2.4、§14（等价性断言）、附录 I2 |
| `store.py` | 286 | §8.3（种子）、§10.2（默认值）、附录 I4 |
| `channel.py` | 157 | §5.2、§6 |
| `app.py` | 151 | 附录 I1 |
| `approver.py` | 137 | §2.1、§4 |
| `events.py` | 53 | §2.2（全文） |
| `__init__.py` | 20 | — |
| **合计** | **1898** | |

前端，`frontend/src/`：

| 文件 | 行数 | 说明 |
|---|---|---|
| `theme.css` | 627 | **一行没改**，从旧控制台原样搬过来的设计令牌和布局 |
| `components/Extensions.tsx` | 656 | Providers / MCP / Skills / Memory / Rules / Plugins 六个面板 |
| `useThread.ts` | 357 | §7 的重连与重拉，加事件归约 |
| `App.tsx` | 288 | 外壳、composer、会话设置抽屉 |
| `console.css` | 263 | 本章新增的样式；一个硬编码颜色都没有，全是 `theme.css` 的令牌 |
| `components/Chat.tsx` | 250 | 对话流、工具块、审批卡、§9 的三种 mark |
| `components/Sidebar.tsx` | 195 | 按工作目录分组 |
| `components/ThreadSettings.tsx` | 185 | §10.2 的 3×3 矩阵和两段式确认 |
| `api.ts` / `types.ts` / `main.tsx` | 285 | |
| **合计** | **3106** | |

工具与测试：

| 文件 | 行数 | |
|---|---|---|
| `tests/test_faults_ch19.py` | 780 | 40 个测试，F19-01…F19-11，全离线 |
| `probe_web.py` | 345 | 四段，三段离线 |
| `probe_mutations_ch19.py` | 306 | 32 个变异，0 存活 |
| `scripts/check_layers.py` | 381 | §8 的两处修复（原本 302 行） |

**没有进正文的部分，以及为什么：**

- **`Extensions.tsx` 的六个面板**，656 行。它们是同一个形状重复六次：拉一个
  接口、渲染一列 `item-card`、一个添加表单。看一个就够了，六个都贴是凑篇幅。
- **`theme.css`**，627 行。**它不是这一章写的**——它是上一版控制台的设计，
  经过两轮批注评审定下来的。这一章对它的贡献是"一行没改"，这件事本身
  比任何一段 CSS 都重要，所以只说这一句。
- **`routes.py` 里 28 个路由中的 22 个**。CRUD 就是 CRUD。正文只讲了
  会出错的那六个：发消息（§3）、答审批（§4）、建 thread（§10.1）、
  改设置（§3.1）、清 memory（§10.2）、fork。
- **React 的常规部分**（`useState`、列表 key、受控输入）。这是一本讲 Agent 的书。

---

## §14 验证

```
$ uv run ruff format --check .
110 files already formatted

$ uv run ruff check .
All checks passed!

$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone

$ uv run pytest
3 failed, 1721 passed, 9 skipped in 109.97s (0:01:49)
```

> 那 3 个 failed 是 `test_shell.py` 和 `test_faults_ch10.py` 里假设 POSIX
> shell（`cat` / `true` / `pwd`）的用例，在 Windows 上红，和本章无关——
> 在 `step18_skills` 上跑同样这 3 个也是红的。CI 跑在 ubuntu 上，全绿。
> 本章新增 40 个（1681 → 1721）。

```
$ npm --prefix frontend run build
> tsc --noEmit && vite build

vite v7.3.6 building client environment for production...
✓ 36 modules transformed.
dist/index.html                   0.70 kB │ gzip:  0.43 kB
dist/assets/index-DWumoQqC.css   23.75 kB │ gzip:  5.06 kB
dist/assets/index-CdBRONZ6.js   230.78 kB │ gzip: 71.08 kB
✓ built in 669ms

$ uv run python probe_mutations_ch19.py
32 mutations, tests/test_faults_ch19.py tests/test_packaging.py tests/test_boundaries.py
...
every mutation was caught.
```

**32 个变异，0 存活。** 其中三个改的是 `scripts/check_layers.py` 自己——
这是这个脚本第一次去变异一个**检查**而不是代码。理由就是 §8：
一个补好的检查，如果没有东西会注意到它退化，那它就在等着重新坏一次。

`probe_web.py bundle` 的代价那一栏：

```
    index-CdBRONZ6.js             230,776 bytes
    index-DWumoQqC.css             23,746 bytes
    index.html                        701 bytes
                                  255,223 bytes total

  from 11 source files, 3,106 lines
  node_modules to build it: 68 entries
```

旧控制台是**一个 1523 行的 HTML 文件**，双击就能开，没有构建步骤。
新的是 255 KB 产物、11 个源文件、68 个 node_modules 顶层目录、
一条新的环境要求。换来的是类型检查（它在第一次构建就抓到了三条故障，
见回头看）、默认转义（旧版把模型产出的字符串拼进 `innerHTML`）、
以及六个新面板还能维护。**这是一笔真实的交易，不是纯赚。**

---

## §15 收工：commit、PR

### commit 序列

```
feat(web): serve the console from `minicodex serve`

The backend is a subpackage, not a second project: `src/minicodex/web/`,
`fastapi`/`uvicorn` behind the `web` extra, and one lazily-imported branch
in `__main__`. Nothing under `src/minicodex/*.py` changed to make it work
-- the seams were already there (ch5 Approver, ch7 RolloutWriter observer
and resolve("last", dir), interlude B's composition root).

fix(web): claim a thread before creating its task, not inside it

F19-01. `_busy.add` ran inside the coroutine handed to create_task, which
does not start until the handler has returned -- so two POSTs arriving
together both passed the check and opened two RolloutWriters on one
directory.

fix(web): resolve an approval on the loop that created its future

F19-02. A `def` FastAPI route runs in an anyio worker thread and
asyncio.Future is not thread-safe. The route is `async def` now, and the
broker goes through call_soon_threadsafe whoever calls it.

fix(web): one queue and one pump per thread, so events keep their order

F19-03, F19-04. Measured before fixing: 0 inversions in 20/20 runs when
every send costs one loop turn, 20/20 when one in three costs two. Not
flaky -- invisible on a laptop and certain on a real network.

fix(scripts): check_layers walks subpackages and resolves relative imports

F19-06, F19-07. Four rules globbed one level, which was right while the
package was flat; chapter 19's `web/` was invisible to all five while the
lint step reported success. Fixing the glob then showed eight modules with
no edges, because a subpackage is written in relative imports and
node.level == 0 skipped every one.

fix(web): break the store -> __main__ cycle the repaired checker found

F19-08. Base URLs from model.py, the two model names declared in store.py,
and a test asserting they still match __main__.PROVIDERS. Plus a forbidden
edge so nothing below the console can import it.

feat(web): render the marks, the plan and mid-turn permission changes

F19-09. compacted / budget_exhausted / interrupted were emitted and dropped
by the browser, so a run could compact half its history in silence. The
plan and the sandbox mode are derived by diffing after each durable item,
not by a hook in agent.py -- which is why core-diff still reports zero.

fix(web): validate a workspace path where it enters the program

F19-10. POST /api/threads took any string and run_turn did
mkdir(parents=True), so a typo created a directory tree anywhere the server
user could write and ran an agent in it.

feat(web): the full 3x3 permission matrix, per thread, on screen

F19-10, F19-11. Sandbox mode and approval policy are settable and always
visible; memory read, memory write and skills default off, read and write
stay separate consents, and writing asks a second time.

feat(frontend): rebuild the console in React, reconnecting this time

F19-05, F19-12. The design is the previous console's, ported unchanged
(theme.css is byte-for-byte the reviewed original). What is new is the
reconnect with a resync, the six settings panels, a document skeleton, and
a type checker that found three faults on its first build.

test(web): 40 offline tests and 32 mutations, three against the checker

docs: chapter 19, FAULTS 207 -> 218, README table and the Node requirement
```

### PR 描述

```markdown
## What

`minicodex serve` — the same agent, driven from a browser. Sessions grouped by
workspace, the full sandbox × approval matrix on screen and editable per
thread, approvals as cards that really suspend the tool call, memory and
skills switched on per session, a stop button, and a fork menu.

## Why

The CLI is the only interface this agent has ever had, and every seam that a
different interface would need was written for another reason years of chapters
ago. Whether those seams actually hold was worth finding out.

## How

`src/minicodex/web/` behind the `web` extra; `frontend/` is React + Vite.
`probe_web.py core-diff` reports **0 agent modules changed** — `__main__.py`
grew 57 lines, all of them the `serve` subcommand.

## Testing

1721 tests (up 40), all offline. 32 mutations, 0 survivors. `check_layers.py`
now covers subpackages, which is how three of this chapter's faults were found.

## What this does not do

- **No token-level streaming.** Measured: per-item costs a reader 2.23s on
  average, 3.64s worst (`probe_web.py stream`, 12 requests, gpt-4o-mini). That
  is the argument for building it, and it would be the first change to the
  agent this chapter did not have to make. Not built.
- **No auth.** Loopback by default, with a warning if you move it.
- **No multi-writer store.** One process, one lock, JSON files.
- **Plugins tab is honest.** There is no plugin module to wire it to.
```

### Code review

**R1：`web/runtime._instructions` 是 `__main__._instructions` 的复制粘贴。
为什么不 import？**

因为 import 它就是 F19-08 那条边——`web` 在 `__main__` **旁边**，不在它下面，
而 `__main__` 是更外层。复制的代价用第 3 章的老办法付：写一个测试比较两者。
而且这个测试第一版是**弱的**，只用默认参数比了一次，四条条件段落
（plan / memory / skills / note）根本没走到。现在四个开关组合全比。

**R2：`_derived` 那个"每条 item 之后 diff 一次"看着很土。为什么不给
`TaskPlan` 加个回调？**

因为加回调就要改 `plan.py`，`core-diff` 就不是零了。而这个"土"办法的代价是
每条 item 一次元组比较——在一个每条 item 都要 fsync 的路径上，这个开销
不可测量。**当一个笨办法的成本低于它省下的耦合时，它就不笨。**

**R3：`ci.yml` 的 Lint 步骤现在同时跑 ruff、check_layers 和 tsc，
Test 步骤同时跑 pytest 和 vite build。一步做两件事。**

**这条我接受一半。** 六步上限是第 -1 章的规矩，`test_F_1_05_ci_is_valid_yaml_and_stays_small`
在断言它，而 `actions/setup-node` 本身就要占一步。所以选择是：折进去，
或者把前端检查挪到非阻塞的 `postmerge.yml`。

选折进去，因为**前端构建失败应该拦住合并**——它是产物的一部分，
和第 9 章把变异测试放进 postmerge 的理由正好相反（那个测的是测试的质量，
不是这次改动的正确性）。

接受的那一半是：这确实破了"一步一件事"，而且 runner 自带的 Node 版本
是一个没有被钉住的依赖。这两点写在 workflow 的注释里，不是藏起来。

**R4：审批卡片上的命令输入框是可编辑的。这不是给了用户一个绕过检查的口子吗？**

不是，而且反过来。`ApprovalReply.command` 从第 5 章起就是"用户实际同意运行的
那条命令"——CLI 也让你改。批准一条**和它请求的略有不同**的命令是一个真实的
回答，而且是比"全批"或"全拒"更常用的那个。它走的还是同一条 gate。

**R5：`store.py` 的 `_save` 做了 fsync，但这是个本地开发工具，值得吗？**

fsync 和并发无关，那是两件事。这里存的是**权限设置**。一个崩溃之后回滚成
空文件的设置文件，意味着一个会话悄悄退回 `read-only`——或者更糟，退回到
一个和界面上显示的不一样的模式。**一个会悄悄回滚的权限，比一个从来没设过的
权限更危险。**

### Merge

Squash。理由和前面每一章一样：这十二个 commit 是一条思路的十二个片段，
`main` 上要的是"控制台这件事"这一个节点。

### CI

`ci.yml` **仍然是 6 步**（`npm ci` 折进 Install dependencies，`tsc` 折进
Lint，`vite build` 折进 Test）。`postmerge.yml` 加了第 11 个变异检查步骤——
`test_F_1_05_every_mutation_script_runs_somewhere` 会盯着这件事，
它在第 12、15、18 章已经抓过作者三次。

---

## §16 对照 codex

| 这里 | codex | 差在哪 |
|---|---|---|
| `src/minicodex/web/` 直接挂 FastAPI，HTTP + WebSocket | `codex-rs/app-server` + `app-server-protocol` + `app-server-transport` + `app-server-client`，一套 JSON-RPC **进程协议** | codex 把界面和核心之间做成了协议边界，TUI 只是这个协议的一个客户端；这里是把 HTTP 直接接在组装根上 |
| `check_layers.py` 的 `* → web` 禁止边 | `verify_tui_core_boundary.py` | 同一条规矩，同一个执行方式：**用 CI 强制，不靠口头约定** |
| 逐 history item 流式，复用 `RolloutWriter` 观察者 | `EventMsg` 事件流，`AgentMessageDelta` 逐 token | codex 有独立的事件类型体系，所以 token 级流式是它本来就有的一条路；这里是白捡了持久性、付了 2.23 秒 |
| memory 开关默认关，写入要二次确认 | `Feature::MemoryTool`，key `"memories"`，`default_enabled: false`（`features/src/lib.rs:995-1000`） | 完全一致，而且是独立得出的：F17-11 和 codex 的默认关是同一条推理 |
| 专用工具 `memory_search`/`memory_read` 默认不挂 | `MemoriesConfig { dedicated_tools: false }`（`config/src/types.rs:344`） | 也一致，但**不是**独立得出的：第 16 章第一版把这对工具当成唯一入口，是照着 codex 改回来的（F16-12） |
| `serve` 绑 loopback，无鉴权 | `app-server` 走 stdio，根本不监听端口 | codex 的做法更彻底：**不监听就不需要鉴权**。这里选了端口，所以欠一条警告和一个默认值 |
| Plugins tab 写着"没有这个东西" | 有插件/bundle 机制 | 这里没有对应模块，所以说实话 |

**最重要的一行是第一行。** codex 把"界面 ↔ 核心"做成了一个**带版本的进程协议**，
代价是要维护一套协议定义、一个传输层、一个客户端库；收益是 TUI、IDE 插件、
云端任务可以是四个不同的进程，各自演进。

这里没有做协议。`web/runtime.py` 直接 `import minicodex.agent`，在同一个进程里
跑同一个循环。这是**一个界面**的正确规模——协议的成本要等到有第二个界面
才开始回本，而这本书还没有第二个界面。

但那条 `* → web` 的禁止边，是替将来那个协议**保住了位置**：只要没有任何东西
从下面 import 上来，把这一层换成一个 JSON-RPC 服务端就仍然是一次局部改动。
codex 用 `verify_tui_core_boundary.py` 守的也正是这个。

---

## §17 回头看：这一章撞到了什么

清单上的 11 条：

| ID | 结果 |
|---|---|
| F19-01 | 复现。而且**第一版测试是假绿的**——对着坏代码也通过，要靠一个"永不结束的 turn" fixture 才测得到 |
| F19-02 | 复现。属于"大部分时候是对的"那一类，所以两道防线 |
| F19-03 | 复现，但**形状和预期不同**：不是"有时候乱"，是 0% 然后 100% |
| F19-04 | 复现。空集合和弱引用的 task，两个方向的同一个问题 |
| F19-05 | 复现。旧代码里那句 `ws.onclose = null` 说明它被想到过 |
| F19-06 | 复现，**而且是一直在复现**——十八章的 lint 步骤对它没读过的代码报告成功 |
| F19-07 | 只有在 F19-06 修好之后才看得见 |
| F19-08 | **由 F19-06/07 的修复在第一分钟抓到**。两条元故障立刻回本 |
| F19-09 | 复现。整个事件类型被前端丢弃 |
| F19-10 | 复现。附带发现"瞎编的沙箱模式不会失败，只会不可预测" |
| F19-11 | 未发生——**因为它是被设计挡住的**，不是被修掉的。默认值是先写的 |

清单外的 8 条：

| 故障 | 发现 | 修法 |
|---|---|---|
| `Omit<Entry, "key">` 在联合类型上塌成公共键 | ⚪ static | 分配式条件类型。`tsc` 第一次构建就抓到 |
| `Recorder(path)` 收的是**文件**路径，传了目录 | 🟣 review | 每个会话一个文件名 |
| `test_F_1_02` 只查 `dev` 组，新的 `web` extra 整个逃掉 | 🟡 silent | 遍历所有组。**和 F19-06 一模一样的形状**：规则写成了查表而不是循环 |
| 重拉之后工具结果渲染了两次 | 🟠 observability | 只有孤儿结果单独成块 |
| `__main__._finish_writer` 还在用内建 `TimeoutError` | ⚪ static | 记下来，不改——那是第 15 章的事 |
| 旧 `index.html` 没有 doctype 和 viewport | 🟠 observability | 移动端抽屉写了但**从来没生效过** |
| 审批卡只能发 `session`，后端一直收 `project` | 🟣 review | 两个按钮 + Rules 面板 |
| 种子 provider 的模型 tag 不存在 | ⚫ user report | 和 CLI 默认值比对的测试 |

发现方式分布（11 条上榜的）：

| 发现方式 | 数量 |
|---|---|
| 🟡 silent | 3 |
| 🟣 review | 3 |
| 🟠 observability | 1 |
| 🔵 long run | 1 |
| ⚫ user report | 1 |
| 🟢 boundary test | 1 |
| ⚪ static | 1 |

**11 条里有 6 条在还没有用户的代码里，而这 6 条里有 3 条指向的是检查而不是控制台。**
这才是这一章真正的发现，而且它和浏览器没有关系。

---

## 如果你只记住三件事

**1. 一个能换掉的界面，是十八章之前每一次"把口子留窄一点"的利息。**

`Approver` 写成 Protocol 是为了测试；`RolloutWriter` 挂在观察者上是为了断电恢复；
`resolve("last", dir)` 收一个目录参数是为了 `--session-dir`；`composition.py`
存在是因为父子装配漂移过一次。**四件事，四个当时的理由，没有一个是"将来要做
浏览器"。** 但它们加起来的结果是这一章改了 0 行 Agent 代码。

反过来说：如果第 5 章当时把 `input()` 直接写在 `gate_command` 里，
这一章的第一件事就是重构审批。**架构的回报从来不在当期兑现，
所以它在当期永远看起来不划算。**

**2. 一条会悄悄停止适用的规则，比没有这条规则更危险。**

`check_layers.py` 用 `glob("*.py")` 找模块，这在十八章里都是对的。
第十九章加了一个子目录，**五条规则一条都不再覆盖新代码，而 CI 一直是绿的**。
`test_F_1_02` 只查 `dev` 一个依赖组，新加的 `web` extra 同样整个逃掉。

同一个形状出现了两次，而且两次都不是"规则写错了"——是规则写成了**一次查找**
而不是**一次遍历**。这类失效不会报错，因为那个绿勾在承诺失效之后
还在做同样的承诺。所以补完之后，变异脚本第一次去变异检查本身：
一个没有东西盯着的修复，是在等着重新坏一次。

**3. 先量，再修——量出来的形状经常和你以为的不一样。**

事件乱序，预期是"偶尔"，实测是 **0% 然后 100%**：在笔记本上完全正确，
在真网络上每次都错。这个形状意味着它是一条**你不可能靠用它发现的 bug**，
而"偶尔 4%"意味着迟早会有人碰上——两个结论会导向完全不同的优先级。

流式那条更直接：逐 item 的代价是**平均 2.23 秒、最坏 3.64 秒**。
量之前，"复用观察者钩子，白送持久性"听起来是纯赚；量完之后它是一笔要写进
PR 的交易。而第一次量出来的是 **-0.19 秒**——一个不可能的结果，
说明实验错了，不是世界错了。

---

## 动手练习

1. **把 `channel.py` 里那句 `await socket.send_text(payload)` 改回
   `create_task(...)`，跑 `tests/test_faults_ch19.py`。** 数一下红了几个。
   然后把 `_Sink` 的 jitter 关掉（`probe_web.py` 里改成永远 `False`），
   再跑 `probe_web.py order`——为什么这次一个逆序都没有？

2. **把 `check_layers.py` 的 `rglob` 改回 `glob`，跑 `scripts/check_layers.py`。**
   它会说什么？现在再故意在 `agent.py` 里加一行 `from minicodex.web import serve`，
   重跑。**它报错了吗？** 这就是十八章里一直存在的那个状态。

3. **把 `DEFAULT_THREAD_SETTINGS` 里的 `"memory"` 改成 `True`，跑测试。**
   哪个测试红了？读一遍它的 docstring，然后回去看第 17 章的 F17-11——
   为什么同一条推理在浏览器里更强而不是更弱？

4. **（难）给控制台加 token 级流式。** `ChatCompletionsModel.stream` 已经会
   yield `TextDelta`。你需要一条新的事件类型、一个前端的增量渲染、
   以及一个决定：这条新路径上的事件，**要不要也保证"到浏览器 iff 已落盘"**？
   如果不保证，`useThread` 的重拉逻辑会发生什么？跑一遍 `probe_web.py stream`
   看你省下了多少。

5. **（难）把 `_derived` 换成给 `TaskPlan` 加一个观察者回调。** 写完之后跑
   `probe_web.py core-diff`——那个"0"变成几了？然后论证一下：为了让 plan
   面板实时更新，这个代价值不值得付。（提示：先量一下 `_derived` 那次元组
   比较在一条 fsync 路径上占多少。）

6. **把 `frontend/src/components/Chat.tsx` 里 `budget_exhausted` 那个分支删掉，
   跑测试。** 再把 `agent.py` 里写这个 mark 的地方改个名，重跑。
   两次的报错信息有什么不同？为什么第二个要单独写一句 assert 消息？

---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

三条地面规则，和前面每一章的附录一样：

1. 本附录只解释第 19 章在 `steps/step19_web_console/` 里**新增**的代码。
   `agent.py`、`tools.py`、`rollout.py` 那些前面章节讲过的东西，
   这里**不再重复**。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成"此处略"。
3. 代码块按当前源码核对。

## I1 · `app.py`：一个 FastAPI 应用是怎么装起来的

如果你没写过 FastAPI，先记住三个词就够读懂这一章的后端了：

- **app**：一个对象，知道有哪些 URL，以及每个 URL 该调哪个函数。
- **router**：一批 URL 的集合，可以整批塞进 app。
- **`app.state`**：一个随便挂东西的地方，请求处理函数能从
  `request.app.state` 拿到它。

这一章的整个装配就是这么三步：

```python
def create_app(data_dir: Path = DEFAULT_DATA_DIR, *, frontend: Path | None = None) -> FastAPI:
    store = Store(data_dir)
    store.seed_providers()

    app = FastAPI(title="minicodex console", version=__version__)
    app.state.console = Console(store=store)      # ← 这一个进程拥有的全部东西
    app.include_router(router)
    ...
```

**为什么是 `app.state.console` 而不是模块级全局变量？**

第一版就是全局变量：`_broker = ApprovalBroker()`、`_subscribers = {}`、
`_busy = set()`，写在模块顶上。它能跑，但它**在一个解释器里只能存在一份**。
于是测试就没法给每个 `tmp_path` 建一个干净的控制台——第二个测试会看到第一个
测试留下的 provider 和 thread。

这和 `composition.py`（插曲 B）存在的理由是同一个：
**一个顺便做 IO 的组装根，是一个你没法从测试里调用的组装根。**

`Console` 就是一个 dataclass，把这个进程拥有的东西装在一起：

```python
@dataclass
class Console:
    store: Store
    channel: Channel = field(default_factory=Channel)
    broker: ApprovalBroker = field(default_factory=ApprovalBroker)
    busy: set[str] = field(default_factory=set)
    running: dict[str, asyncio.Task[None]] = field(default_factory=dict)
```

**静态文件为什么挂在最后？**

```python
    if dist.is_dir() and (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
```

Starlette 按**注册顺序**匹配路由。`app.mount("/", ...)` 会接管所有 URL，
所以它必须在 `include_router(router)` 之后——先注册的 `/api/...` 和 `/ws/...`
先被认领，剩下的才落到静态文件上。反过来写的话，你的 API 会全部返回 404，
而且是那种"文件不存在"的 404，不是"没有这个路由"的。

## I2 · `runtime.py`：一次 turn 的顺序，为什么不能乱

`run_turn` 是本章最长的函数，但它没有一处是新发明的——它是
`__main__._ask` 的同构。有几个顺序是**必须**的，写错了不会报错：

**（1）`registry` 要在 `top_level_tools` 之后建。**

```python
    tools = top_level_tools(root, session, sub_ctx, plan=..., memory=..., ...)
    registry = McpRegistry(local=tools.schemas)      # ← 要上一行的结果
```

因为 registry 拿的是**那个 list 对象本身**，`tool_search` 靠往里 append
来揭示一个 schema，下一次请求就自动带上了（第 9 章）。

**（2）压缩的 summariser 要在 `llm` 之后绑，而且要绑两处。**

```python
    llm = make_model(tools.schemas)
    if context_window:
        wiring = replace(wiring, summariser=make_summariser(llm))
        sub_ctx = replace(sub_ctx, wiring=wiring)     # ← 这一行是插曲 B 的教训
```

只绑 `wiring` 不绑 `sub_ctx`，父 Agent 会压缩、子 Agent 永远不压缩——
那正是插曲 B 测出来的 181,040 vs 76,896 字符。**不会报错，只会贵。**

**（3）后台的 memory writer 要在第一次请求之前起。**

```python
    writer_task = asyncio.ensure_future(run_pipeline(make_model([]), ...))
    ...
    result = await agent.run(question)
```

第 17 章的 F17-01：放在**运行结束之后**，实测是答案出来到提示符回来之间
多等 14 秒，而用户没要过这个。放在**旁边**，它消耗的是**之前的**会话
（这一轮还没结束，按名字排除掉了）。

注意 `make_model([])`——**空工具列表**。writer 是两次没有工具的模型调用，
把这一轮的 schema 给它就是每次请求白付一遍那些 token。

**（4）系统提示词里，权限那段必须在最后。**

```python
    parts.append(block)      # permissions_block
    return "\n\n".join(parts)
```

第 13 章的 F13-07：provider 按**前缀**缓存提示词，而权限是这个字符串里
唯一会在会话中途变的部分（`request_permissions` 会重写它）。放前面，
它一变就把整个缓存作废。

## I3 · `routes.py`：一个路由长什么样

```python
@router.post("/api/threads/{thread_id}/messages", status_code=202)
async def send_message(request: Request, thread_id: str, body: SendMessage) -> dict[str, Any]:
    console = _console(request)
    record = console.require_thread(thread_id)
    if not body.text.strip():
        raise HTTPException(400, "message cannot be empty")

    if thread_id in console.busy:
        raise HTTPException(409, "this thread is still answering the previous message")
    console.busy.add(thread_id)
    ...
```

逐行：

- `@router.post(...)`：把这个函数注册成 `POST /api/threads/{id}/messages`。
  花括号里的名字会作为参数传进来（`thread_id: str`）。
- `status_code=202`：成功时返回 202 Accepted 而不是 200 OK。
  **202 的意思是"收到了，还没做完"**——这正好是这个接口的语义：
  turn 是在后台跑的，响应回去的时候它才刚排上队。
- `body: SendMessage`：`SendMessage` 是一个 pydantic 模型，
  FastAPI 会自动把请求的 JSON 解析成它，字段不对就自动返回 422。
- `raise HTTPException(400, "...")`：这就是返回一个 400 的方式。
  那句话会出现在响应的 `detail` 字段里，前端的 `api.ts` 就是读它的——
  所以用户看到的是 `no such directory: /tpm/foo`，不是一个数字。

**`async def` 还是 `def`？** 这不是风格问题，见 §4：`def` 的路由会跑在
线程池里。**任何要碰 `asyncio` 对象的路由都必须是 `async def`。**

## I4 · `store.py`：原子地写一个 JSON 文件

```python
    def _save(self, name: str, data: Any) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
```

四步，每一步都有理由：

1. **写到 `.tmp`**，不直接写目标文件。直接写的话，写到一半崩溃就得到一个
   截断的 JSON——而截断的 JSON 读回来是解析失败，也就是"设置全没了"。
2. **`flush()`**：把 Python 的用户态缓冲推给操作系统。
3. **`os.fsync(fd)`**：让操作系统把它真正落到盘上。没有这一步，
   `replace` 之后掉电，你可能得到一个**存在但内容是空的**文件——
   这比文件不存在更糟，因为不存在会退回默认值，空文件会解析失败。
4. **`tmp.replace(path)`**：`rename` 在 POSIX 和 Windows 上都是原子的。
   任何时刻去读，要么读到完整的旧版本，要么读到完整的新版本。

还有第五步，best-effort：

```python
        try:
            fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
```

`rename` 本身也是一次**目录**修改，目录不 fsync 它也不持久。
Windows 不允许用 `O_RDONLY` 打开目录，所以这一步失败就跳过——
**一个在 Windows 上起不来的控制台，比一个在 Windows 上丢一秒钟写入的
控制台，是更严重的故障。**

## I5 · `channel.py`：队列 + 泵是什么意思

如果你没写过 asyncio，"一个队列一个泵"这句话可能不直观。拆开：

```python
    def emit(self, thread_id: str, event: dict[str, Any]) -> None:
        queue = self._queues.get(thread_id)
        if queue is None:
            return
        queue.put_nowait(json.dumps(event, ensure_ascii=False, default=str))
```

`emit` 是**同步**函数，它只做一件事：把一段文本塞进队列。
`put_nowait` 不会阻塞也不会 `await`——所以它可以从任何地方调用，
包括 `History` 的观察者回调那种"不能 await"的地方。

```python
    async def _pump(self, thread_id: str, queue: asyncio.Queue[str | None]) -> None:
        while True:
            payload = await queue.get()
            if payload is None:
                return
            for socket in list(self._sockets.get(thread_id, ())):
                try:
                    await socket.send_text(payload)
                except Exception:
                    self._sockets.get(thread_id, set()).discard(socket)
```

泵是一个**一直在跑的协程**：从队列里取一条，发给所有 socket，再取下一条。
关键在于它是**一个**协程——所以"取第 2 条"这件事必然发生在"第 1 条发完"
之后。顺序是这个结构本身保证的，不是靠调度器帮忙。

`list(...)` 那一层拷贝也是必须的：循环体里可能会 `discard` 一个 socket，
在迭代过程中改动一个集合，Python 会抛 `RuntimeError: Set changed size
during iteration`。

## I6 · React 那边：`useThread` 这个 hook 在做什么

React 的 hook 就是一个普通函数，名字以 `use` 开头，可以在组件里调用，
并且能"记住"东西。`useThread(threadId)` 返回一个对象，组件拿它渲染：

```typescript
const live = useThread(threadId);
// live.entries  → 要画的对话流
// live.busy     → 输入框是不是该禁用
// live.send(text) → 发一条消息
```

里面三个东西值得单独说：

**`useState` vs `useRef`。** `useState` 变了会触发重新渲染；`useRef` 不会。
所以"要画在屏幕上的东西"（entries、busy、connection）用 `useState`，
"只是内部记账"（socket 对象、重试次数、定时器 id）用 `useRef`——
用错了会导致每次重试都把整个页面重画一遍。

**`useEffect` 的清理函数。** 这个 return 出来的函数是组件卸载或依赖变化时
跑的：

```typescript
    return () => {
      cancelled = true;
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        socket.onclose = null;   // ← 主动关闭，不要触发重连
        socket.close();
      }
    };
```

`socket.onclose = null` 这一行是**必须**的，而且它和 §7 里嘲笑过的那一行
长得一模一样——区别是这里 `onclose` 真的被赋过值。不清掉它，
主动切换 thread 时的那次 close 会被重连逻辑当成掉线，然后往一个已经不看的
thread 上重连。

**为什么 `entries` 里每项都有一个 `key`。** React 用 key 判断"这一项还是
上次那一项吗"。用数组下标当 key，在列表**中间插入**时会让 React 认为
后面每一项都变了——表现是输入框失焦、展开状态丢失。所以这里用一个
全局自增的 `e1`、`e2`：

```typescript
let seq = 0;
const nextKey = () => `e${++seq}`;
```

## I7 · 新手常见报错 / 坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| `minicodex serve` 说 `the console needs its extra` | 只装了基础依赖 | `uv sync --all-extras`，或 `uv sync --extra web` |
| 打开 8000 端口看到 "The console has no build yet" | 前端没构建 | `npm --prefix frontend ci && npm --prefix frontend run build` |
| `npm run dev` 页面能开，但发消息 404 | 后端没起，或者起在别的端口 | 另开一个终端 `uv run minicodex serve`；代理写死在 `vite.config.ts` 里指向 8000 |
| `npm run dev` 下 WebSocket 连不上，HTTP 却正常 | `vite.config.ts` 的 `/ws` 那条少了 `ws: true` | 加上。没有它代理会用普通 HTTP 响应回应升级请求，表现是"连上了然后没声音" |
| 审批点了没反应，600 秒后显示超时 | 路由被写成了 `def` | 改成 `async def`，见 §4 |
| 对话流偶尔顺序不对 | 用了 `create_task` 扇出 | 见 §5。注意在笔记本上大概率复现不出来 |
| 服务器 reload 之后页面卡在 `thinking…` | 没有 `onclose`/重连 | 见 §7 |
| `check_layers.py` 报 `no such file` 之类的怪错 | 在错误的目录跑 | 它默认 `src/minicodex`，要在 step 根目录跑 |
| 加了新模块之后 `check_layers.py` 还是全绿 | **可能是它根本没看见**（F19-06） | 确认用的是 `rglob` 版本；故意加一条禁止边试试它会不会红 |
| `test_F_1_02` 突然红了 | 新加的依赖没写下界 | 每个依赖都要 `>=`，**每个 extra 组都要**（F19-06 的同款） |
| `pytest` 里 WebSocket 测试挂住 | `TestClient` 没用上下文管理器 | `with TestClient(app) as client:`，否则 lifespan 不跑 |
| 前端 `tsc` 报 `Property 'text' does not exist` | 在联合类型上用了 `Omit` | 用分配式条件类型，见回头看表第一行 |
| 改了 `agent.py` 里 mark 的名字，测试红了 | 这是设计如此 | 报错信息会告诉你去更新 `test_F19_09_...` |
