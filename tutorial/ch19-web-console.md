# 第 19 章 · Web 控制台：换一个人机界面，Agent 一行都不用改

> **代码**：`steps/step19_web_console/`
> **分支**：`feat/web-console`
> **产出**：后端子包 `src/minicodex/web/`（8 个模块）、`minicodex serve` 子命令、前端 `frontend/`（React + Vite）、`scripts/check_layers.py` 的两处修复、`probe_web.py` 五个测量段
> **前置**：做完第 18 章。本章 54 个测试全部不联网；`probe_mutations_ch19.py` 不联网；`probe_web.py` 的 `core-diff` / `order` / `bundle` / `browser` 四段不联网。只有 `probe_web.py stream` 要真的请求 OpenAI——这次改写时手边没有 key，§12 的数字是第一次写这一章时量的，照原样保留并注明
> **本章新增一条环境要求**：前端要 Node ≥ 20。后端和所有 pytest 测试都不要。`probe_web.py browser` 还要 Playwright（一个能用代码驱动浏览器的库）
>
> **阅读顺序在第 15 章之前，编号在其之后。** 理由和第 16–18 章一样。
>
> **这一章很长，可以分四次读**：§0–§2 是概念、骨架和后端的组装；§3–§6 是"一轮对话在后台跑"带来的四个并发问题；§7 是整个前端；§8 往后是检查器、标记、校验、测量和收工。

---

## §0 开工前的准备

前面十八章，这个程序只有一种用法：在终端里敲一条命令，等它答完。这一章给它一个**网页界面**。这意味着你会第一次碰到一堆和"Agent"本身无关、但写网页程序绕不开的东西。下面先把它们一次讲清楚，正文里就不再停下来解释。

### 0.1 本章会遇到的新概念

**网络和服务器**

- **HTTP**：浏览器和服务器说话的方式。浏览器发一个**请求**（"给我 `/api/threads`"），服务器回一个**响应**（一段 JSON 和一个状态码）。一问一答，答完就结束。
- **HTTP 方法**：`GET` 是"读"，`POST` 是"新建 / 做一件事"，`PATCH` 是"改一部分"，`DELETE` 是"删"。
- **状态码**：响应里的那个数字。`200` 成功；`202` "收到了，还在做"；`400` "你发的东西不对"；`404` "没有这个"；`409` "现在不行（冲突）"；`422` "格式不对"（FastAPI 自动给）；`500` 服务器自己出错了。
- **JSON**：一种文本格式，长得像 Python 的字典和列表。浏览器和服务器之间传的数据几乎都是它。
- **API / 路由（route）**：服务器上每一个"地址 + 方法"对应一个函数。`GET /api/threads/abc` 调用的是 `get_thread(thread_id="abc")`。这样一个对应关系叫一个路由。
- **WebSocket**：HTTP 是一问一答，服务器不能主动说话。WebSocket 是一条**一直开着**的连接，服务器想说什么随时往里写。这一章用它把 Agent 正在做的事实时推给浏览器。
- **localhost / 127.0.0.1 / 端口**：`127.0.0.1` 是"本机"，只有这台电脑自己能连；端口是一台机器上区分不同服务的数字（这一章默认 8000）。

**Python 这一侧的新工具**

- **FastAPI**：一个写 HTTP 服务的 Python 库。你写普通函数，在上面加一个"装饰器"说明它对应哪个地址，FastAPI 负责收请求、解析 JSON、调函数、把返回值变回 JSON。
- **pydantic**：FastAPI 用它描述"请求里应该有哪些字段、是什么类型"。你写一个类，FastAPI 就按它检查请求，不对就自动回 422。
- **uvicorn**：真正监听端口、把网络上的字节交给 FastAPI 的那个程序（一个"服务器"）。
- **ASGI**：FastAPI 和 uvicorn 之间约定的接口。测试里的 `TestClient` 直接走这个接口，不用真的开端口。
- **事件循环（event loop）**：第 8 章的 `asyncio` 那一套。这一章所有请求、WebSocket、后台跑的那一轮对话，**都在同一个事件循环上轮流执行**。
- **`asyncio.Future`**：一个"以后才会有结果的盒子"。有人 `await` 它就会停在那儿，直到别人往盒子里放了结果（`set_result`）。这一章的审批就是这么等用户点按钮的。
- **`asyncio.Queue`**：一个排队用的队列。一头 `put`，另一头 `await get()`，取出来的顺序就是放进去的顺序。
- **`asyncio.Task`**：一个被交给事件循环"在后台跑"的协程。`create_task` 之后它就自己跑，你的代码接着往下走。
- **线程（thread）和事件循环的区别**：事件循环是**一个**线程里轮流跑很多协程；线程是操作系统真的同时跑的另一条执行线。`asyncio` 的大部分东西**不能**从别的线程里碰——§4 整节就是这件事。

**浏览器这一侧的新工具**

- **前端 / 后端**：在浏览器里跑的那部分程序叫前端（这一章是 TypeScript + React），在服务器上跑的叫后端（Python）。它们之间只通过 HTTP 和 WebSocket 说话。
- **TypeScript**：带类型的 JavaScript。写法和 Python 的类型标注很像：`text: string`、`busy: boolean`。它在**构建时**检查类型，浏览器里跑的是去掉类型之后的 JavaScript。
- **React**：一个画网页的库。你写一个**组件**（一个返回"页面应该长什么样"的函数），数据一变，React 就重新调用它、把变化画到屏幕上。
- **JSX / TSX**：在 TypeScript 里直接写类似 HTML 的标签：`<div className="msg">{item.text}</div>`。花括号里是普通表达式。
- **hook**：React 里以 `use` 开头的函数，让组件"记住"东西。`useState` 记一个值，值变了就重画；`useRef` 记一个值，变了**不**重画；`useEffect` 在画完之后做一件事（比如连 WebSocket），并在不需要时清理掉。
- **Vite**：把一堆 `.tsx` 文件打包成浏览器能直接加载的几个文件（放在 `frontend/dist/`）。开发时它还能起一个自动刷新的服务器。
- **npm**：JavaScript 的包管理器，相当于 Python 这边的 `uv`/`pip`。`package.json` 是依赖清单，`package-lock.json` 是锁定的确切版本（相当于 `uv.lock`）。

**这一章特有的几个词**

- **控制台（console）**：这一章做的网页界面。
- **thread（会话线程）**：控制台里的一段对话。磁盘上就是一个目录，里面每一轮对话写一个第 7 章的 rollout 文件。
- **turn（一轮）**：用户发一条消息，到 Agent 答完，算一轮。
- **事件（event）**：后端通过 WebSocket 推给浏览器的一条 JSON，比如"多了一条历史记录""需要审批""这一轮完了"。
- **快照（snapshot）/ 重新同步（resync）**：浏览器断线重连或刷新之后，用 `GET /api/threads/{id}` 把整段对话重新拉一遍，叫重新同步。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `@router.get("/api/x")` 写在 `def` 上面 | 装饰器：把下面这个函数注册成 `GET /api/x` 的处理函数 |
| `def f(thread_id: str)`，路由是 `"/api/threads/{thread_id}"` | 地址里花括号的部分会作为同名参数传进来 |
| `class NewThread(BaseModel): workspace: str` | pydantic 模型：描述请求体里的 JSON 字段。FastAPI 自动解析、检查 |
| `raise HTTPException(400, "...")` | 回一个 400 响应，那句话放在响应的 `detail` 字段里 |
| `request.app.state.console` | FastAPI 应用对象上的一个"随便挂东西"的地方 |
| `@dataclass` 里的 `field(default_factory=set)` | 每个实例一个**新的**空集合（直接写 `= set()` 会让所有实例共用一个） |
| `loop.create_future()` / `future.set_result(x)` / `await future` | 造一个"以后才有结果"的盒子 / 放进结果 / 等结果 |
| `asyncio.wait_for(x, timeout=600)` | 最多等 600 秒，超时抛 `asyncio.TimeoutError` |
| `loop.call_soon_threadsafe(f, x)` | 从**别的线程**请事件循环"找个时间在你自己的线程里调用 `f(x)`" |
| `queue.put_nowait(x)` / `await queue.get()` | 不等待地放进队列 / 等到有东西再取出 |
| `task.add_done_callback(fn)` | 任务结束时调用 `fn(task)` |
| `contextlib.suppress(A, B)` | `with` 里面抛 `A` 或 `B` 就当没发生 |
| `uuid.uuid4().hex[:12]` | 一个随机的、几乎不可能重复的字符串，取前 12 位当 id |
| `os.fsync(handle.fileno())` | 让操作系统把文件内容真正写到磁盘上（第 7 章） |
| `tmp.replace(path)` | 把 `tmp` 改名成 `path`，原子地覆盖（第 7 章） |
| `shlex.split("npx -y server")` | 像 shell 一样把一行命令切成参数列表 `["npx", "-y", "server"]` |
| `{**a, **b}` | 合并两个字典，`b` 里的同名键覆盖 `a` 的 |
| `Protocol`（`typing`） | "只要有这几个方法就算"的类型，不用继承（第 5 章） |
| `subprocess.run([...], capture_output=True, text=True)` | 启动一个外部程序并拿回它的输出（`probe_web.py browser` 用它启动浏览器脚本） |

### 0.3 本章会遇到的 TypeScript / React 写法

只列读懂这一章代码所需的最少几条。

| 写法 | 意思 |
|---|---|
| `const x = 1;` / `let y = 2;` | 定义变量。`const` 不能再赋值，`let` 可以 |
| `(a: number) => a + 1` | 箭头函数，相当于 Python 的 `lambda a: a + 1`（但可以写多行） |
| `` `e${n}` `` | 模板字符串，相当于 Python 的 `f"e{n}"` |
| `type A = { kind: "note"; text: string } \| { kind: "mark"; mark: string }` | 联合类型：要么是这种形状，要么是那种。用 `kind` 区分 |
| `interface X { a: string; b?: number }` | 描述一个对象的形状。`?` 表示可以没有 |
| `x ?? y` | `x` 是 `null`/`undefined` 就用 `y` |
| `x?.y` | `x` 是 `null`/`undefined` 就得到 `undefined`，而不是报错 |
| `[...list, item]` / `{ ...obj, key: v }` | 展开：复制一个列表再加一项 / 复制一个对象再改一个键（不改原来的） |
| `list.map(f)` / `list.filter(f)` / `list.some(f)` / `list.findIndex(f)` | 和 Python 的列表推导、`any()`、找下标是一回事 |
| `async function f() { await g(); }` | 和 Python 的 `async def` / `await` 一样 |
| `promise.then(f).catch(g).finally(h)` | 一个异步结果出来以后做 `f`，出错做 `g`，无论如何最后做 `h` |
| `const [v, setV] = useState(0)` | React：一个会触发重画的值，和改它的函数 |
| `const r = useRef(0); r.current = 1` | React：一个不触发重画的盒子 |
| `useEffect(() => { ...; return () => {...} }, [deps])` | React：画完之后做事；`deps` 变了就先跑 `return` 的清理函数，再重做一遍 |
| `useCallback(fn, [deps])` | React：同一个 `fn` 在 `deps` 不变时保持是同一个函数对象 |
| `<Chat entries={entries} />` | 使用一个组件，把 `entries` 传给它（叫 props） |
| `{cond && <div/>}` | `cond` 为真才画后面那个 |
| `key={entry.key}` | 列表里每一项一个稳定的身份，React 靠它判断"这还是上次那一项吗" |

### 0.4 开分支，装依赖

```bash
git switch main
git pull
git switch -c feat/web-console

cd steps/step19_web_console
uv sync --all-extras                        # 后端，包括 web 这个可选依赖组
npm --prefix frontend ci                    # 前端依赖，按 package-lock.json 装确切版本
npm --prefix frontend run build             # 打包到 frontend/dist/
uv run minicodex serve                      # http://127.0.0.1:8000
```

`npm --prefix frontend` 的意思是"在 `frontend/` 目录里执行"，省得来回 `cd`。

### 0.5 这次给新手改写时，找到了什么

这一章的第一版写了 40 个测试、32 条变异，全部通过。这次改写除了逐行重读代码，还做了一件第一版没做过的事：**用一个真的浏览器去用它**——起服务器、打开页面、发两条消息、数屏幕上有什么（`probe_web.py browser`，§7.10）。结果：

```
                                          you  agent  tool calls
  live, after "what does this define?"      2      2           1
  live, after "and again?"                  5      5           2
  after a reload                            2      3           1
```

**发第二条消息之后，页面上把第一轮整段又画了一遍**：5 个"你"、5 个"minicodex"、2 个工具块，而刷新之后是正确的 2、3、1。第一条消息本身也被画了两次。所有测试都是绿的，因为没有一个测试问过"一个人在浏览器里看到了什么"。

一共找到六处，都修了，各自在正文里有一节：

1. **续接时把整段历史重新推了一遍**，浏览器又画了一遍（§7.11）。
2. **页面第一次加载时先拉快照、后订阅**——恰好是代码注释里警告过的那个缝（§7.11）。
3. **刷新之后，等着审批的那张卡片不见了**，那一轮却还在等它，等 10 分钟（§4.3）。
4. **最后一个 socket 刚走、下一个就来**，新来的那个永远收不到事件（§6.2）。
5. **网页那份 `_finish_writer` 没收到第 17 章改写的修复**（§2.7）。这是"修复到了 `__main__`、没到 `web/`"的第三次。
6. **`"memory": "false"` 会把记忆打开**——`bool("false")` 是 `True`（§10.3）。

还有一处**没修**，而是指给第 23 章：控制台的记忆面板读的是 `<工作区>/.minicodex/memories`，而每一轮对话读的是第 16 章搬过去的 `~/.minicodex/memories`。于是面板显示的不是 Agent 用的记忆，"全部忘掉"也删不掉 Agent 用的那份。第 23 章（F23-05）专门有一节讲它是怎么被发现的，这里提前修掉会让那一节失去意义。§10.4 会把它指出来。

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

baseline: steps/step18_skills/src/minicodex

    31 top-level modules byte-identical to chapter 18
     3 changed only by chapter 15's fixes: registry.py, replay.py, subagent.py
     0 agent modules changed for the console  <- the headline

  __main__.py: +36 lines, all of it the `serve` subcommand
  new backend  (8 modules): 2019 lines
  new frontend (9 files):   2291 lines
```

（这次改写在 Linux 上重跑的。第一版的数字是 30 / 4 / +57 行。之后好几次改写的修复是同时打进第 18 章和第 19 章两个快照的——`git log -- steps/step18_skills/src/minicodex/agent.py` 能看到——所以两边相同的文件多了，`__main__.py` 的差别少了。"0"没有变。）

**零。** 31 个顶层模块和第 18 章逐字节相同。唯一动过的是 `__main__.py`，多了 36 行，全部是 `serve` 这个子命令的参数和分发——那是**入口**学会了一件新事，不是 Agent 变了。

那 3 个"只被第 15 章的修复改过"的文件下面 §11 会解释。

先说清楚这个零是怎么来的，因为它不是运气。

---

## §2 三条接缝，都是别人为别的理由留的

浏览器需要三件 CLI 不需要的东西：**把审批问出去、把过程播出去、把对话接回来**。
这三件事各自需要一个口子。三个口子都已经在了。

### 2.1 审批：`Approver` 是个 Protocol（第 5 章）


```python
class Approver(Protocol):
    """Anything that can answer a yes/no about running something.

    A Protocol for the same reason `Model` is one: the tests need a
    deterministic implementation today, not hypothetically.  Structural typing
    means the test doubles below inherit nothing.
    """

    async def ask(self, request: ApprovalRequest) -> ApprovalReply: ...
```

（`approval.py`，第 5 章写的。）


第 5 章把它写成 Protocol，理由和浏览器没有一点关系：当时是为了让测试能塞一个
`AllowAll()` 进去，不用真的读 stdin。但一个只有一个 `async` 方法的协议，
换成"挂在一个 `asyncio.Future` 上，等一个后来的 HTTP 请求来解开它"是天然成立的：


`web/approver.py` 里的 `WebApprover.ask` 就是这样（全文在 §4.1）：

```python
async def ask(self, request: ApprovalRequest) -> ApprovalReply:
    approval_id = uuid.uuid4().hex[:12]
    event = {
        "type": "approval_request",
        "id": approval_id,
        "what": request.what,
        "reason": request.reason,
        "risk": request.risk.name,
        "suggested_rule": (
            list(request.suggested_rule) if request.suggested_rule is not None else None
        ),
    }
    future = self._broker.register(approval_id, thread=self._thread, event=event)
    self._emit(event)
    try:
        reply = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS)
```

> 造一个 id、在"等待表"里登记一个 Future、把请求作为一个事件发给浏览器、然后 `await` 那个 Future——最多 600 秒。浏览器点了按钮，另一个 HTTP 请求会把结果放进这个 Future（§4）。

`agent.py` 一行没动。`approval.py` 一行没动。`policy.py` 一行没动。

### 2.2 过程：`RolloutWriter.append` 就是 `History` 的观察者钩子（第 7 章）

第 7 章为了断电之后还能接上，让 `History` 每接受一条 item 就回调一次
`observer`，`RolloutWriter` 就挂在这个回调上。


```python
class StreamingRolloutWriter(RolloutWriter):
    """A `RolloutWriter` that also says, out loud, what it just wrote."""

    def __init__(self, path: Path, meta: SessionMeta, emit: EventSink) -> None:
        super().__init__(path, meta)
        self._emit = emit
        #: Called after each item is durable, to let the run report state that
        #: is not itself a history item -- the plan, and the sandbox mode.
        #: Assigned by `runtime.run_turn`; see `_derived` there.
        self.after_item: Callable[[], None] | None = None
        #: How many items the history this file loads back as holds so far.
        #: `read_rollout` keeps what follows the last `compacting` marker, so
        #: that marker is where counting starts again.
        self._position = 0

    def append(self, item: Any) -> None:  # type: ignore[override]
        super().append(item)
        index, self._position = self._position, self._position + 1
        self._emit({"type": "history_item", "index": index, "item": _dump_item(item)})
        if self.after_item is not None:
            self.after_item()

    def mark(self, kind: str, **payload: Any) -> None:  # type: ignore[override]
        super().mark(kind, **payload)
        if kind == "compacting":
            self._position = 0
        self._emit({"type": "mark", "mark": kind, "payload": payload, "position": self._position})
```

（`web/events.py`，全文在 §9.1。`index` 和 `position` 那几行是这次改写加的，§7.11 讲为什么。）

> - `class StreamingRolloutWriter(RolloutWriter)`：**继承**第 7 章的写入器。`super().append(item)` 先让父类把这条写进文件、fsync，然后才发事件。
> - `self._emit`：一个"发事件"的函数，由创建它的人传进来（§2.6 会看到是谁）。它不知道 WebSocket，只知道"调用它，把字典交出去"。
> - `after_item`：每写完一条，调一下这个回调（§2.6 的 `_derived`）。


这个子类买到的东西比看上去多。它保证了一条性质：

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


（`web/runtime.py` 里的这几行，§2.6 有它们的上下文。）

```python
task_plan = TaskPlan()
tools = top_level_tools(
    root, session, sub_ctx, plan=task_plan, memory=memory, remember=remember, skills=skills
)
```

```python
agent = wiring.agent(
    llm,
    tools,
    instructions=_instructions(session, tools, memory=memory, skills=skills),
    rollout=writer,
    resume_from=resume_from,
    on_stop=unfinished_note(task_plan),
    on_turn_start=_on_turn_start,
)
```

一行都不是新写的。**如果插曲 B 没做，这一章要么改 `__main__`，
要么复制一份装配逻辑然后慢慢和它漂移。**


### 2.5 代码：先把骨架搭起来

动手写控制台之前，先把三件和 Agent 无关的事做完：依赖、入口、存储。

**（一）依赖：一个可选组。** `pyproject.toml` 里加一个叫 `web` 的可选依赖组：

```toml
# Chapter 19.  An extra rather than a runtime dependency because the agent runs
# perfectly well without a browser attached, and `fastapi` + `uvicorn` pull in
# roughly forty packages to serve one page.  `uv sync --all-extras` (what CI
# runs) installs it; `pip install minicodex` still gets an agent and a CLI.
web = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
]
```

> - **可选组（extra）**：`pip install minicodex` 不会装它；`pip install 'minicodex[web]'` 或 `uv sync --extra web` 才装。CI 跑的是 `uv sync --all-extras`，全装。
> - `uvicorn[standard]`：uvicorn 自己也有可选组，`standard` 带上 WebSocket 支持。

加了一个组，第 -1 章那个"每个依赖都要写下界"的测试也得跟着改——它原来只查 `dev` 这一个组：

```python
def test_F_1_02_every_dev_dependency_has_a_lower_bound(repo_root: Path) -> None:
    """An unbounded dependency lets CI and a laptop resolve different versions
    from the same commit, which makes 'works on my machine' unfalsifiable.

    Every optional group, not just `dev`.  This read `["dev"]` by name for
    eighteen chapters, which was correct while `dev` was the only group there
    was -- and then chapter 19 added `web` with two more dependencies in it,
    and the rule did not extend to them because the rule had been written as a
    lookup rather than as a loop.  The same shape as F19-06 one file over: a
    check that silently stops covering new code, while still passing.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    with (repo_root / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    groups = cfg["project"]["optional-dependencies"]
    assert "dev" in groups, "the test runner has to come from somewhere"
    for name, specs in groups.items():
        assert specs, f"empty dependency group: {name}"
        for spec in specs:
            assert any(op in spec for op in (">=", "==", "~=")), f"unbounded: {name} -> {spec}"
```

> 说明文字里讲了它的故事：原来写的是 `["dev"]`——**按名字查一个**，而不是**挨个查所有**。于是新加的 `web` 组整个漏掉，测试还是绿的。§8 会看到同一个形状在检查器里再出现一次。

**（二）入口：`minicodex serve`。** `__main__.py` 多三处。一个常量：

```python
# Written here rather than imported from `minicodex.web.store`, because
# importing anything under `minicodex.web` imports `fastapi`, and `fastapi` is
# an optional extra.  A default value for a flag is not worth making the whole
# CLI refuse to start without the console's dependencies installed; the actual
# import happens inside the `serve` branch, where it is allowed to fail.
CONSOLE_DATA_DIR = Path(".minicodex") / "console"
```

一个子命令：

```python
# Chapter 19.  Beside `serve-stub` rather than under it: both serve HTTP and
# neither is the agent, but the stub replaces a *provider* and this replaces
# the *terminal*.
console = sub.add_parser("serve", help="run the web console on a local port")
console.add_argument(
    "--host",
    default="127.0.0.1",
    help="loopback by default: this process runs commands and has no auth",
)
console.add_argument("--port", type=int, default=8000)
console.add_argument(
    "--data-dir",
    type=Path,
    default=CONSOLE_DATA_DIR,
    help="where the console keeps providers, threads and their rollout files",
)
```

和它的分发：

```python
if args.command == "serve":
    # Imported here and not at the top: `minicodex.web` needs `fastapi`,
    # which is the `web` extra.  Every other subcommand has to keep working
    # on an install that does not have it.
    try:
        from minicodex.web import serve as serve_console
    except ImportError as exc:
        print(f"the console needs its extra: uv sync --extra web ({exc})", file=sys.stderr)
        return 1

    return serve_console(host=args.host, port=args.port, data_dir=args.data_dir)
```

> - **`from minicodex.web import serve` 写在 `if` 里面，不写在文件顶上。** `minicodex.web` 一导入就要导入 `fastapi`；没装 `web` 组的人，连 `minicodex ask` 都会起不来。放在这个分支里，只有真的要起控制台时才需要它，没装就打印一句该怎么装。
> - `--host` 默认 `127.0.0.1`：只有本机能连。这个进程会替连上它的人跑命令、改文件，而它**没有任何登录**（第 23 章才有）。

**（三）包的入口：`web/__init__.py`。**

```python
"""The browser front end for this agent: `minicodex serve`.

The outermost layer of the package, beside `__main__` rather than under it.
Nothing in `src/minicodex/*.py` imports anything from here -- that direction is
what `scripts/check_layers.py` checks, and it is why the console could be
written at all without touching the agent: every seam it needs was already
there for another chapter's reason.

    approval.Approver          chapter 5   ->  a browser instead of `input()`
    rollout.RolloutWriter      chapter 7   ->  live events, fsynced first
    rollout.resolve("last")    chapter 7   ->  one conversation per thread
    composition.top_level_tools interlude B ->  one place that assembles a run
"""

from __future__ import annotations

from .app import Console, create_app, serve
from .store import DEFAULT_DATA_DIR, Store

__all__ = ["DEFAULT_DATA_DIR", "Console", "Store", "create_app", "serve"]
```

> 说明文字是这一章的一张地图：控制台要的四个口子，分别是哪一章、为了什么别的理由留下的。`from .app import ...` 里的点是**相对导入**：从同一个包（`minicodex.web`）里导入。§8.2 会看到它让检查器栽了个跟头。

**（四）存储：`web/store.py`。** 控制台要记住的东西——有哪些模型提供方（provider）、哪些 MCP 服务器、哪些会话——都放在一个目录下的几个 JSON 文件里。先是说明和常量：

```python
"""Local, file-backed state for the console: providers, MCP servers, threads.

Plain JSON files under one data directory, one process, one lock. This is a
local dev tool driving one agent from one browser, not a multi-user service --
`steps/step07_resume` and `steps/step08_concurrency` already show what a real
multi-writer store needs (an append-only file, an `O_EXCL` lock, single-writer
discipline), and reaching for that here would be solving a problem this
deployment does not have.

What it *does* borrow from those chapters is the durability discipline, because
that part is not about concurrency: `_save` writes a temporary file, fsyncs it,
renames it over the target and then fsyncs the directory. The rename is atomic
on both platforms this book runs on; without the fsyncs it is atomic with
respect to *other processes* and not with respect to power loss, which is the
distinction chapter 7 spends a section on (F07-03). A settings file that comes
back empty after a crash is a console that silently reverts to `read-only`,
and a permission that quietly reverts is worse than one that was never set.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from minicodex.model import OLLAMA_BASE_URL, OPENAI_BASE_URL
from minicodex.policy import APPROVAL_POLICIES, SANDBOX_MODES

#: What the console offers on a fresh install, one entry per provider the CLI
#: knows about.
#:
#: Not imported from `__main__.PROVIDERS`, and that is not a style preference:
#: it was written that way first and `scripts/check_layers.py` reported an
#: import cycle, `__main__ -> web -> web.app -> web.routes -> web.store ->
#: __main__`. `__main__` is the layer *above* this one, and a lower layer
#: reaching up for a constant is how the cycle chapter 1 opened with (F01-08)
#: gets rebuilt with different names.
#:
#: So the base URLs come from `model.py`, which is where provider facts already
#: live, and the two model names are declared here -- with a test asserting
#: this table and `__main__.PROVIDERS` still agree. Chapter 3 made the same
#: repair for the tool descriptions: when two places must say the same thing
#: and neither can import the other, the cheapest way to make them agree is a
#: test that compares them.
SEED_PROVIDERS: tuple[dict[str, Any], ...] = (
    {
        "name": "Ollama (local)",
        "provider": "ollama",
        "base_url": OLLAMA_BASE_URL,
        "model": "gemma4:31b-cloud",
    },
    {
        "name": "OpenAI",
        "provider": "openai",
        "base_url": OPENAI_BASE_URL,
        "model": "gpt-4o-mini",
    },
)

#: Where `minicodex serve` keeps everything it owns, under the working
#: directory's `.minicodex/` like every other file this program writes.
DEFAULT_DATA_DIR = Path(".minicodex") / "console"

#: Per-thread defaults. `read-only` and `on-request` are the CLI's defaults
#: (`__main__._add_permission_flags`) and they are the console's too: a browser
#: is not a reason to start with more permission than a terminal does.
#: Memory and skills default **off** for the reason chapter 17 gives in F17-11
#: -- switching either on changes what every later turn sends, and a default-on
#: switch is not a consent. The console asks twice before memory writing, which
#: is the same shape codex's TUI uses for `Enable memories?`.
DEFAULT_THREAD_SETTINGS: dict[str, Any] = {
    "sandbox_mode": "read-only",
    "approval_policy": "on-request",
    "context_window": None,
    "memory": False,
    "remember": False,
    "skills": False,
}
```

> - **`SEED_PROVIDERS`**：第一次启动时放进去的两个提供方。注释讲了为什么这里自己写一份、而不是从 `__main__` 导入——§8.3 的 F19-08。
> - **`DEFAULT_DATA_DIR`**：`.minicodex/console`，相对当前目录。
> - **`DEFAULT_THREAD_SETTINGS`**：每个会话的默认设置。沙箱 `read-only`、审批 `on-request`，和命令行的默认一样；记忆和技能**默认关**（§10.2）。

`_new_id` 和设置校验（§10 会讲为什么要校验）：

```python
def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Reject a sandbox mode or policy the core does not define.

    The frontend renders these from `GET /api/policy`, so a bad value can only
    arrive from a handwritten request -- but `Session.mode` is a `Literal` that
    nothing checks at runtime, and an unknown mode does not fail: it falls
    through every branch in `policy.judge_command` and lands on the default.
    Which default that is depends on the branch, so an invented mode is not
    "denied", it is *unpredictable*. Checked here, where the string enters the
    program, rather than where it is eventually compared.
    """
    merged = {**DEFAULT_THREAD_SETTINGS, **settings}
    if merged["sandbox_mode"] not in SANDBOX_MODES:
        raise ValueError(f"unknown sandbox mode {merged['sandbox_mode']!r}")
    if merged["approval_policy"] not in APPROVAL_POLICIES:
        raise ValueError(f"unknown approval policy {merged['approval_policy']!r}")
    window = merged["context_window"]
    if window is not None:
        window = int(window)
        if window <= 0:
            raise ValueError("context window must be a positive number of tokens")
        merged["context_window"] = window
    for flag in ("memory", "remember", "skills"):
        # Refused, not coerced. The first version did `bool(value)`, and
        # `bool("false")` is `True`: a request that spelled the switch as a
        # string turned memory *on* -- a consent F17-11 says must be explicit,
        # granted by the word for refusing it. Found when chapter 19 was
        # rewritten.
        if not isinstance(merged[flag], bool):
            raise ValueError(f"{flag} must be true or false, not {merged[flag]!r}")
    return merged
```

> - `{**DEFAULT_THREAD_SETTINGS, **settings}`：先放默认值，再用传进来的覆盖。所以只传一部分也行。
> - `int(window)`：前端传来的可能是字符串或数字，统一变成整数；不是正数就拒绝。
> - 最后那个 `for` 是这次改写改过的（§10.3）。

`Store` 类。类说明讲了为什么是一个对象而不是模块级的全局变量：

```python
class Store:
    """Every JSON file the console owns, under one directory.

    An object rather than module-level globals, because `minicodex serve
    --data-dir` exists and because the tests need a store per `tmp_path`. The
    module-level version of this was written first and could not be tested
    twice in one interpreter without leaking state between the two runs.
    """
```

构造函数和读写文件的几个底层方法：

```python
def __init__(self, data_dir: Path = DEFAULT_DATA_DIR) -> None:
    self.data_dir = Path(data_dir)
    self.sessions_dir = self.data_dir / "sessions"
    self._lock = threading.RLock()


def _path(self, name: str) -> Path:
    return self.data_dir / name


def _load(self, name: str, default: Any) -> Any:
    path = self._path(name)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A corrupt file degrades to the default rather than taking the
        # server down on import. Chapter 16's `load` makes the same call
        # for the same reason: the absence of a thing is not an error.
        return default


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
    # The rename itself is a directory entry, and it is not durable until
    # the directory is. Best-effort: Windows refuses `O_RDONLY` on a
    # directory, and a console that will not start on Windows is a worse
    # fault than a settings file that loses a second of writes there.
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
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

> - `threading.RLock()`：一把"可重入"的锁。FastAPI 把 `def`（不是 `async def`）的路由放到线程池里跑（§4），所以两个请求可能真的同时读写同一个文件。拿着锁才读改写。"可重入"是说同一个线程拿着它的时候再拿一次不会卡死——下面 `add` 里调用 `_load` 和 `_save` 都在锁里。
> - `_load` 读坏了就返回默认值，不让一个损坏的文件把服务器挡在门外。

通用的增删改查。每个文件里是一个列表，每一项是一个带 `id` 的字典：

```python
def all(self, name: str) -> list[dict[str, Any]]:
    with self._lock:
        return list(self._load(name, []))


def get(self, name: str, item_id: str) -> dict[str, Any] | None:
    with self._lock:
        return next((r for r in self._load(name, []) if r["id"] == item_id), None)


def add(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
    with self._lock:
        records = list(self._load(name, []))
        record = {"id": _new_id(), **record}
        records.append(record)
        self._save(name, records)
        return record


def update(self, name: str, item_id: str, **changes: Any) -> dict[str, Any] | None:
    with self._lock:
        records = list(self._load(name, []))
        updated = None
        for record in records:
            if record["id"] == item_id:
                record.update(changes)
                updated = record
        # Only write when something actually changed. The first version
        # rewrote the file on every miss, so a 404 was indistinguishable
        # from a successful update in the file's mtime.
        if updated is not None:
            self._save(name, records)
        return updated


def delete(self, name: str, item_id: str) -> bool:
    with self._lock:
        records = list(self._load(name, []))
        kept = [r for r in records if r["id"] != item_id]
        if len(kept) == len(records):
            return False
        self._save(name, kept)
        return True
```

> - `next((r for r in ... if r["id"] == item_id), None)`：找第一个 id 对上的，没有就是 `None`。
> - `{"id": _new_id(), **record}`：新记录先放一个 id，再放调用者给的字段。
> - `update` 只在真的找到了才写文件（注释里讲了第一版的问题）。

提供方和会话：

```python
def providers(self) -> list[dict[str, Any]]:
    return self.all("providers.json")


def active_provider(self) -> dict[str, Any] | None:
    return next((p for p in self.providers() if p.get("active")), None)


def activate_provider(self, provider_id: str) -> bool:
    with self._lock:
        records = list(self._load("providers.json", []))
        if not any(r["id"] == provider_id for r in records):
            return False
        for record in records:
            record["active"] = record["id"] == provider_id
        self._save("providers.json", records)
        return True


def seed_providers(self) -> None:
    """Something runnable on first launch, so the console is not empty.

    Both entries are switched **off** until one is chosen: the console
    refuses to send anything without an active provider, which is one
    deliberate click rather than a default that quietly picks a model and a
    billing account for you.

    Neither carries a key. The OpenAI entry reads `OPENAI_API_KEY` at call
    time (`runtime.resolve_api_key`), so a key never reaches this file from
    the environment -- clearing the form in the browser cannot copy it in.
    """
    if self.providers():
        return
    for seed in SEED_PROVIDERS:
        self.add("providers.json", {**seed, "api_key": None, "active": False})


def thread_dir(self, thread_id: str) -> Path:
    return self.sessions_dir / thread_id


def new_thread(
    self, workspace: str, title: str, settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    record = self.add(
        "threads.json",
        {
            "workspace": workspace,
            "title": title,
            "created": time.time(),
            "settings": validate_settings(settings or {}),
        },
    )
    self.thread_dir(record["id"]).mkdir(parents=True, exist_ok=True)
    return record


def thread(self, thread_id: str) -> dict[str, Any] | None:
    record = self.get("threads.json", thread_id)
    if record is None:
        return None
    # Older records predate a setting; merging the defaults in on read
    # means a new switch does not have to migrate the file.
    record["settings"] = {**DEFAULT_THREAD_SETTINGS, **record.get("settings", {})}
    return record


def workspaces(self) -> list[dict[str, Any]]:
    """Group threads by working directory -- the shape the sidebar wants."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in sorted(
        self.all("threads.json"), key=lambda r: r.get("created", 0), reverse=True
    ):
        record["settings"] = {**DEFAULT_THREAD_SETTINGS, **record.get("settings", {})}
        grouped.setdefault(record["workspace"], []).append(record)
    return [{"path": path, "threads": items} for path, items in grouped.items()]
```

> - **`seed_providers`**：只在一个都没有的时候放种子，而且两个都是**未启用**——"用哪个模型、花谁的钱"要用户点一下才算数。
> - **`new_thread`**：存一条记录，再建 `sessions/<id>/` 目录。每一轮对话的 rollout 文件写在这个目录里。
> - **`thread`**：读出来时把默认设置合并进去——以后加一个新开关，旧记录不用迁移。
> - **`workspaces`**：按工作目录分组，侧边栏要的就是这个形状。`sorted(..., key=lambda r: r.get("created", 0), reverse=True)` 是新的在前。

几个测试守着存储：

```python
def test_the_store_survives_a_corrupt_file(tmp_path: Path) -> None:
    """A settings file that comes back unreadable must degrade to the defaults,
    not take the server down on start-up."""
    store = Store(tmp_path)
    store.add("providers.json", {"name": "x"})
    (tmp_path / "providers.json").write_text("{ not json", encoding="utf-8")
    assert store.providers() == []


def test_the_store_writes_atomically(tmp_path: Path) -> None:
    """No half-written file, and no `.tmp` left behind to be read as one."""
    store = Store(tmp_path)
    store.add("threads.json", {"workspace": "/tmp", "title": "t"})
    assert list(tmp_path.glob("*.tmp")) == []
    assert json.loads((tmp_path / "threads.json").read_text(encoding="utf-8"))
```

**（五）应用：`web/app.py`。** 把存储、事件通道、审批表、路由装成一个 FastAPI 应用。说明和导入：

```python
"""Assemble the console: one store, one channel, one broker, one router.

A `Console` object rather than module-level globals, for the reason every other
composition root in this package exists (`composition.py`, interlude B): state
that is created once per process and reached for by name is state that cannot
be created twice in one interpreter, which means it cannot be tested. Every
route reaches its dependencies through `request.app.state.console`, so a test
builds an app on a `tmp_path` and gets a whole console with nothing shared.

The static mount is last and at `/`. Starlette matches routes in registration
order, so every `/api/...` and `/ws/...` above is claimed first and only what
nothing else wanted falls through to the built frontend.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from minicodex import __version__

from .approver import ApprovalBroker
from .channel import Channel
from .routes import router
from .store import DEFAULT_DATA_DIR, Store

log = logging.getLogger(__name__)
```

一个"还没构建前端"时显示的页面（一段 HTML 字符串，`{version}` 这些在 `.format` 时填进去；`{{`、`}}` 是字面上的花括号）：

```python
#: Where `npm run build` puts the console. Two directories up from this module
#: is `src/minicodex`, so this resolves to the step's own `frontend/dist`.
FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

_NO_BUILD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>minicodex console - not built</title>
<style>
  body {{ font: 15px/1.6 ui-sans-serif, system-ui, sans-serif; max-width: 46rem;
         margin: 12vh auto; padding: 0 1.5rem; color: #10182B; background: #EDF1FA; }}
  code {{ background: #fff; border: 1px solid #CCD6EC; border-radius: 6px;
          padding: .15em .4em; font-family: ui-monospace, Consolas, monospace; }}
  pre {{ background: #fff; border: 1px solid #CCD6EC; border-radius: 9px; padding: 1rem;
         overflow-x: auto; }}
  h1 {{ font-size: 1.4rem; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #EAF0FF; background: #0A1120; }}
    code, pre {{ background: #15203A; border-color: #26365C; }}
  }}
</style></head><body>
<h1>The console has no build yet</h1>
<p>The API is up &mdash; this is <code>minicodex serve</code> {version} answering. What is
missing is the frontend bundle, which is not committed to the repository.</p>
<pre>cd {root}
npm ci
npm run build</pre>
<p>Then reload this page. While working on the frontend itself, run
<code>npm run dev</code> instead and use the Vite server on port 5173; it proxies
<code>/api</code> and <code>/ws</code> back here.</p>
<p>Expected at: <code>{dist}</code></p>
</body></html>"""
```

`Console`——这个进程拥有的全部东西：

```python
@dataclass
class Console:
    """Everything one `minicodex serve` process owns."""

    store: Store
    channel: Channel = field(default_factory=Channel)
    broker: ApprovalBroker = field(default_factory=ApprovalBroker)
    #: Threads with a turn in flight. Claimed synchronously in the route, which
    #: is the whole point -- see routes.py.
    busy: set[str] = field(default_factory=set)
    #: The task per busy thread, so the stop button has something to cancel and
    #: so nothing collects a running task.
    running: dict[str, asyncio.Task[None]] = field(default_factory=dict)

    def require_thread(self, thread_id: str) -> dict[str, Any]:
        record = self.store.thread(thread_id)
        if record is None:
            raise HTTPException(404, f"no thread {thread_id!r}")
        return record
```

**为什么是 `app.state.console` 而不是模块级全局变量？**

第一版就是全局变量：`_broker = ApprovalBroker()`、`_subscribers = {}`、
`_busy = set()`，写在模块顶上。它能跑，但它**在一个解释器里只能存在一份**。
于是测试就没法给每个 `tmp_path` 建一个干净的控制台——第二个测试会看到第一个
测试留下的 provider 和 thread。

这和 `composition.py`（插曲 B）存在的理由是同一个：
**一个顺便做 IO 的组装根，是一个你没法从测试里调用的组装根。**

> - `field(default_factory=Channel)`：每个 `Console` 一个新的 `Channel`。
> - **`busy`**：哪些会话正在跑一轮（§3）。**`running`**：每个正在跑的会话对应的那个 `Task`，停止按钮要它（§3.2）。
> - `require_thread`：找不到就直接回 404。几乎每个路由第一行都调它。

`create_app` 和 `serve`：

```python
def create_app(data_dir: Path = DEFAULT_DATA_DIR, *, frontend: Path | None = None) -> FastAPI:
    store = Store(data_dir)
    store.seed_providers()

    app = FastAPI(title="minicodex console", version=__version__)
    app.state.console = Console(store=store)
    app.include_router(router)

    dist = FRONTEND_DIST if frontend is None else Path(frontend)

    @app.on_event("shutdown")
    async def _stop_everything() -> None:
        console: Console = app.state.console
        # Cancel in-flight turns before the loop goes away. Each one is inside
        # `agent.run`, which writes `mark("interrupted")` on the way out, so
        # Ctrl-C on the server leaves resumable threads rather than truncated
        # ones -- the same guarantee chapter 7 gives the CLI.
        for task in list(console.running.values()):
            task.cancel()
        for task in list(console.running.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        console.broker.fail_all("server shutting down")
        await console.channel.aclose()

    if dist.is_dir() and (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:

        @app.get("/", response_class=HTMLResponse)
        def _needs_a_build() -> str:
            # A page that says what to run, not a 404. The first thing anyone
            # does with a new checkout is start the server and open the port.
            return _NO_BUILD_PAGE.format(version=__version__, root=dist.parent, dist=dist)

    return app


def serve(host: str = "127.0.0.1", port: int = 8000, data_dir: Path = DEFAULT_DATA_DIR) -> int:
    """`minicodex serve`. Bound to localhost by default, deliberately.

    This process runs shell commands and edits files on behalf of whoever can
    reach it, with an approval prompt as the only gate, and it has no
    authentication of any kind. `0.0.0.0` is a decision for a person to make on
    purpose, not a default to inherit.
    """
    try:
        import uvicorn
    except ImportError:
        print("the console needs its extra: uv sync --extra web")
        return 1

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "listening on %s: this server runs commands and edits files, and has no auth", host
        )
    uvicorn.run(create_app(data_dir), host=host, port=port, log_level="info")
    return 0
```

> - **`app.include_router(router)`**：把 `routes.py` 里定义的所有路由挂上去。
> - **`@app.on_event("shutdown")`**：服务器关闭时执行。先取消所有正在跑的轮次（`agent.run` 会在退出时写一个 `interrupted` 标记，下次接着跑），再拒绝所有等待中的审批，最后停掉事件泵。
> - `except (asyncio.CancelledError, Exception): pass`：等每个被取消的任务真的结束，不管它是怎么结束的。

**静态文件为什么挂在最后？**

```python
    if dist.is_dir() and (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
```

Starlette 按**注册顺序**匹配路由。`app.mount("/", ...)` 会接管所有 URL，
所以它必须在 `include_router(router)` 之后——先注册的 `/api/...` 和 `/ws/...`
先被认领，剩下的才落到静态文件上。反过来写的话，你的 API 会全部返回 404，
而且是那种"文件不存在"的 404，不是"没有这个路由"的。

> - **`serve`**：`import uvicorn` 放在函数里，原因和 `__main__` 那里一样。不是本机地址就打一条警告。`uvicorn.run(...)` 会一直阻塞到你按 Ctrl-C。

### 2.6 代码：`runtime.py`——一轮对话怎么装起来

这是控制台最重要的一个文件：浏览器发来一条消息，它负责装出一个和 `minicodex ask` 一模一样的 Agent，跑一轮。先看说明，它讲清楚了"一模一样"到哪为止：

```python
"""One user turn, wired exactly the way `minicodex ask` wires one.

This module is the console's whole claim to being real, so it is worth stating
what "exactly" means and where it stops.

Same as `__main__._ask`: the `Wiring`, the recorder, the compaction summariser
and the fact that it is bound to *both* the parent wiring and `sub_ctx`, the
`tool_context`, `top_level_tools` with plan/memory/remember/skills, the MCP
registry and its elicitation handler, `watching(with_remote_tools(...))`, the
system-prompt assembly order, the three `on_turn_start` watchers that deliver
the AGENTS.md, memory and skill blocks, the background memory writer started
before the first request, the resume path including the damage note and the
environment note, and every post-run report.

Different, and only these four:

1. The approver is `WebApprover` rather than `CliApprover` -- chapter 5's seam,
   used, not modified.
2. `announce` and every post-run line go to an event sink instead of `print`.
3. "Continue this conversation" means resuming from *this thread's own* last
   rollout file rather than from a `--resume` flag, so one browser tab can
   never resume into another tab's conversation.
4. The run happens in the workspace the thread was created for, not in the
   process's own cwd. `ShellSession` and `tool_context` already take a root;
   nothing here chdirs, because a server that chdirs has one working directory
   for every thread it is running.

Nothing in `src/minicodex/*.py` outside this subpackage was changed to make any
of it work; `probe_web.py core-diff` is what checks that claim rather than this
docstring making it.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from minicodex import system_prompt
from minicodex.agent import RunResult, Wiring
from minicodex.agent_types import ToolSet
from minicodex.agents_md import AgentsMdWatcher
from minicodex.approval import Session, permissions_block
from minicodex.compaction import make_summariser
from minicodex.composition import sub_context, top_level_tools, watching, with_remote_tools
from minicodex.history import History
from minicodex.mcp import ServerConfig
from minicodex.memory import (
    DEFAULT_MEMORY_DIR,
    Memory,
    MemoryError_,
    MemoryWatcher,
    memory_instructions,
    parse_citations,
    record_uses,
)
from minicodex.memory import load as load_memory
from minicodex.memory_jobs import DEFAULT_JOBS_PATH
from minicodex.memory_write import NOTE_NAME, remember_instructions, run_pipeline
from minicodex.model import ChatCompletionsModel
from minicodex.plan import PLAN_INSTRUCTIONS, TaskPlan, unfinished_note
from minicodex.recorder import Recorder
from minicodex.registry import McpRegistry, connect, elicitation_handler
from minicodex.retry import ModelFailed
from minicodex.rollout import (
    RolloutError,
    SessionMeta,
    environment_note,
    interrupted_note,
    new_session_id,
    read_rollout,
    resolve,
    rollout_path,
)
from minicodex.rules import RuleStore
from minicodex.skills import DEFAULT_SKILLS_DIR, Skills, SkillsWatcher, skills_instructions
from minicodex.skills import discover as discover_skills
from minicodex.subagent import describe_children
from minicodex.tools import TOOL_SCHEMAS, tool_context

from .approver import ApprovalBroker, WebApprover
from .events import EventSink, StreamingRolloutWriter

#: The same grace `__main__` gives the background memory writer once the answer
#: is on screen, and for the same reason.
WRITER_GRACE_SECONDS = 30.0
```

> 导入很长，因为它把前十八章的东西都用上了。每一个都是前面某一章的，这里没有新东西。

**系统提示词。** `__main__._instructions` 的一份**复制**——为什么不导入，说明里讲了（§8.3 的那条禁止边）：

```python
def _instructions(
    session: Session,
    tools: ToolSet | None = None,
    memory: Memory | None = None,
    skills: Skills | None = None,
) -> str:
    """`__main__._instructions`, verbatim in behaviour.

    Duplicated rather than imported because importing it would make this
    subpackage depend on `__main__`, and `__main__` is the layer *above*
    composition, not beside it -- `scripts/check_layers.py` forbids the edge.
    The duplication is checked instead of promised: `test_faults_ch19.py`
    asserts the two produce the same string for the same inputs, which is the
    same repair chapter 3 made for the tool descriptions.

    Both halves take the loaded `Memory`/`Skills` rather than a pair of bools,
    which is chapter 16's rewrite arriving here: the memory paragraph now names
    the directory it describes, so a flag no longer carries enough to build it.
    """
    can_request = any(tool["function"]["name"] == "request_permissions" for tool in TOOL_SCHEMAS)
    block = permissions_block(session, can_request=can_request)
    parts = [system_prompt().rstrip()]
    if tools is not None and "update_plan" in tools.handlers:
        parts.append(PLAN_INSTRUCTIONS)
    if memory is not None:
        dedicated = tools is not None and "memory_search" in tools.handlers
        parts.append(memory_instructions(memory.directory, dedicated_tools=dedicated))
    if skills is not None:
        skill_tool = tools is not None and "read_skill" in tools.handlers
        parts.append(skills_instructions(skill_tool=skill_tool))
    if tools is not None and NOTE_NAME in tools.handlers:
        parts.append(remember_instructions())
    parts.append(block)
    return "\n\n".join(parts)
```

复制的东西会悄悄走样，所以有一个测试把两份在各种组合下逐字比较：

```python
def test_the_web_runtime_builds_the_same_system_prompt_as_the_cli() -> None:
    """`web/runtime._instructions` is a copy of `__main__._instructions`,
    because importing the original would be the F19-08 edge again. A copy that
    nothing compares is a copy that drifts -- chapter 3's tool descriptions,
    one more time.
    """
    from minicodex.__main__ import _instructions as cli_instructions
    from minicodex.composition import local_tools
    from minicodex.memory import Memory
    from minicodex.plan import TaskPlan, plan_toolset
    from minicodex.skills import Skill, Skills
    from minicodex.web.runtime import _instructions as web_instructions

    # Every flag, not just the defaults. The first version of this test called
    # both functions with no tools and no features, so the four conditional
    # paragraphs -- plan, memory, skills, note -- were compared to nothing and
    # either copy could have lost one without a word.
    #
    # `Memory`/`Skills` values rather than the bools this used to pass: after
    # chapter 16's rewrite the memory paragraph names the directory it
    # describes, so a bool no longer carries enough to build one (F16-11).
    memories = (None, Memory(directory=Path("memories"), summary="a summary"))
    skill_sets = (
        None,
        Skills(
            directory=Path("skills"),
            skills=(Skill(name="one", description="a skill", body="body", path=Path("one")),),
        ),
    )
    for mode in SANDBOX_MODES:
        for policy in APPROVAL_POLICIES:
            for memory in memories:
                for skills in skill_sets:
                    session = Session(mode=mode, policy=policy)
                    tools = local_tools(Path.cwd(), session).plus(plan_toolset(TaskPlan()))
                    assert web_instructions(session, tools, memory, skills) == cli_instructions(
                        session, tools, memory, skills
                    )
```

> 四重循环：有没有计划工具、记忆、技能、记笔记工具，每种组合都比一次。注释里说了第一版的测试只比了默认参数——四段条件文字一段都没走到。

**记忆和技能开关。** 把会话设置里的开关变成真正加载好的东西：

```python
def load_feature_dirs(
    root: Path, settings: dict[str, Any]
) -> tuple[Memory | None, Path | None, Skills | None, list[str]]:
    """Resolve the memory and skills switches against one workspace.

    The two resolve *differently*, and after chapter 16's rewrite that is the
    whole point of this function rather than an inconsistency in it. Memory is
    global -- `~/.minicodex/memories`, codex's `codex_home.join("memories")`
    (`memories/read/src/lib.rs:13-15`) -- so every workspace this server hosts
    shares one, exactly as every project on one machine shares one in codex.
    Skills stayed per-project, so they still resolve against the workspace
    root, the thing that path was always relative *to*.

    A server is where that difference is visible for the first time: the CLI
    has one cwd and cannot tell a global directory from a relative one that
    happens to resolve there.

    Returns the notices as data rather than printing them: an empty memory
    directory degrading to "off" is something the browser has to be told, and
    `__main__` tells the terminal at exactly this point.
    """
    notices: list[str] = []
    memory: Memory | None = None
    if settings.get("memory"):
        try:
            loaded = load_memory(DEFAULT_MEMORY_DIR)
        except MemoryError_ as exc:
            notices.append(f"memory: not loaded -- {exc}")
        else:
            if loaded:
                memory = loaded
            else:
                notices.append(f"memory: {loaded.describe()}; running without it")

    # `--remember` without `--memory` is a real configuration: writing is a
    # separate consent from reading (F17-11), so this is not an `elif`.
    remember = DEFAULT_MEMORY_DIR if settings.get("remember") else None

    skills: Skills | None = None
    if settings.get("skills"):
        found = discover_skills(root / DEFAULT_SKILLS_DIR)
        if found.skipped:
            notices.append(f"skills: skipped {', '.join(found.skipped)}")
        if found:
            skills = found
        else:
            notices.append(f"skills: {found.describe()}; running without them")
    return memory, remember, skills, notices
```

> - 记忆从 `DEFAULT_MEMORY_DIR`（`~/.minicodex/memories`）读，所有工作区共用一份；技能从**这个工作区**的 `.minicodex/skills` 读。说明里讲了为什么两者不一样。
> - 出了问题不打印，而是收集成 `notices` 返回——调用它的人把它们作为事件发给浏览器。

**后台记忆写入的收尾**（第 17 章）——这次改写改过，§2.7：

```python
async def _finish_writer(task: asyncio.Task[Any], *, grace: float = WRITER_GRACE_SECONDS) -> str:
    """`__main__._finish_writer`, with the 3.10 timeout spelling corrected.

    The original catches the builtin `TimeoutError` around an `asyncio.wait_for`
    -- which on 3.10 is an unrelated class, so the grace period does not end,
    it raises. Same fault as F15-02, one module over, still there.

    And `Exception` at both points, which is chapter 17's rewrite arriving one
    copy late. That rewrite found the writer's failures escaping `__main__`'s
    version by two routes -- a provider error is the client's own
    `ModelHTTPError` or an `httpx` error, not one of the four types listed, and
    `wait_for` re-raises the task's exception before the second `try` is ever
    reached -- and fixed `__main__`. This copy still had the four types, so in
    the console a failed note-taking job turned an answered turn into an
    `error` line and lost every report after it. Found when chapter 19 was
    rewritten.
    """
    if not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), grace)
        except asyncio.TimeoutError:
            task.cancel()
            return f"memory writer: still going after {grace:.0f}s, left for the next run"
        except Exception:
            pass  # reported below, from the task itself
    try:
        return (await task).describe()
    except asyncio.CancelledError:
        return "memory writer: stopped, left for the next run"
    except Exception as exc:
        return f"memory writer: failed, nothing written ({type(exc).__name__}: {exc})"
```

**`run_turn`：一轮。** 参数是路由给的：会话目录、工作区、问题、设置、用哪个模型、MCP 服务器列表、审批表、发事件的函数。开头：

```python
async def run_turn(
    *,
    thread_dir: Path,
    workspace_root: Path,
    question: str,
    settings: dict[str, Any],
    provider_name: str,
    base_url: str,
    model: str,
    api_key: str | None,
    mcp_servers: list[dict[str, Any]],
    broker: ApprovalBroker,
    emit: EventSink,
) -> RunResult:
    # Already absolute, already resolved, already known to exist: `routes.
    # _validated_workspace` did all three when the thread was created. This
    # function used to do `resolve()` and `mkdir(parents=True)` itself, which
    # is how a typed-in typo silently created a directory tree anywhere the
    # server user could write and then ran an agent inside it. Validation
    # belongs where the string enters the program, not where it is used.
    root = workspace_root
    context_window = settings.get("context_window")

    session = Session(
        mode=settings["sandbox_mode"],
        policy=settings["approval_policy"],
        rules=RuleStore(root / ".minicodex" / "rules.json"),
        approver=WebApprover(broker, emit, thread=str(thread_dir)),
    )

    def make_model(tools: list[dict[str, Any]]) -> ChatCompletionsModel:
        return ChatCompletionsModel(base_url=base_url, model=model, api_key=api_key, tools=tools)

    def announce(message: str) -> None:
        emit({"type": "notice", "text": message})

    memory, remember, skills, notices = load_feature_dirs(root, settings)
    for notice in notices:
        announce(notice)

```

> - `Session(...)`：第 5 章的权限会话。`approver=WebApprover(...)` 是和命令行唯一不同的地方。`thread=str(thread_dir)` 告诉它"是哪个会话在问"（§4.3）。
> - `RuleStore(root / ".minicodex" / "rules.json")`：第 5 章"总是允许"的规则存在这个工作区里。
> - `make_model`：给一张工具表就造一个模型客户端。
> - `announce`：Agent 想对用户说一句话（命令行里是 `print`），这里变成一个 `notice` 事件。

录制器、工具上下文、`Wiring`、会话元数据、子 Agent 上下文：

```python
# A path to a *file*, not a directory: `Recorder(path=None)` builds
# `DEFAULT_DIR / f"session-{int(time.time())}.jsonl"` relative to the
# process cwd, and the process cwd is not the workspace here.
recorder = Recorder(root / ".minicodex" / "recordings" / f"session-{int(time.time())}.jsonl")
context = tool_context(root=root, session=session)
wiring = Wiring(
    recorder=recorder,
    context_window=context_window,
    announce=announce,
    summariser=None,
)
current = SessionMeta(
    session_id=new_session_id(),
    created=time.time(),
    cwd=str(root),
    provider=provider_name,
    model=model,
    sandbox_mode=session.mode,
    approval_policy=session.policy,
)
sub_ctx = sub_context(
    build_model=make_model,
    root=root,
    session=session,
    parent_shell=context.shell,
    wiring=wiring,
    sessions_dir=thread_dir,
    parent_session_id=current.session_id,
    provider=provider_name,
    model=model,
    announce=announce,
)

```

> 每一行都是 `__main__._ask` 里同样的一行。`sessions_dir=thread_dir`：子 Agent 的会话文件也写进这个会话的目录。

工具表、MCP 服务器、压缩：

```python
task_plan = TaskPlan()
tools = top_level_tools(
    root, session, sub_ctx, plan=task_plan, memory=memory, remember=remember, skills=skills
)

registry = McpRegistry(local=tools.schemas)
clients = []
enabled = [s for s in mcp_servers if s.get("enabled", True)]
if enabled:
    configs = [
        ServerConfig(
            name=s["name"],
            command=tuple(s["command"]),
            env=dict(s.get("env") or {}),
            cwd=s.get("cwd"),
            startup_timeout=float(s.get("startup_timeout") or 30.0),
            tool_timeout=float(s.get("tool_timeout") or 60.0),
        )
        for s in enabled
    ]
    clients = await connect(
        configs, registry, handlers={"elicitation/create": elicitation_handler(session)}
    )
    for name, reason in registry.failures.items():
        emit({"type": "mcp_status", "name": name, "status": "error", "detail": reason})
    for client in clients:
        offered = sum(1 for r in registry.registrations.values() if r.client is client)
        emit(
            {
                "type": "mcp_status",
                "name": client.config.name,
                "status": "connected",
                "tools": offered,
            }
        )
    if registry.deferred:
        announce(f"mcp: {len(registry.deferred)} tool(s) deferred behind tool_search")

tools = watching(with_remote_tools(tools, registry), task_plan)
llm = make_model(tools.schemas)
if context_window:
    wiring = replace(wiring, summariser=make_summariser(llm))
    # Both, not just the parent: interlude B's measured drift is exactly a
    # child whose wiring differs from its parent's, and a child that never
    # compacts is the version of it that costs money.
    sub_ctx = replace(sub_ctx, wiring=wiring)

```

> - `connect(...)` 连上每个启用的 MCP 服务器（第 9 章）；连接成功或失败都作为 `mcp_status` 事件告诉浏览器。
> - **有两个顺序是必须的，写错了不会报错：**
>   1. `McpRegistry(local=tools.schemas)` 要在 `top_level_tools` **之后**：它拿的是那个列表**对象本身**，`tool_search` 往里追加一个工具描述，下一次请求就带上了（第 9 章）。
>   2. 压缩用的总结器（`make_summariser(llm)`）要在 `llm` 造出来**之后**绑定，而且要绑两处：`wiring` 和 `sub_ctx`。只绑 `wiring`，父 Agent 会压缩、子 Agent 永远不压缩——插曲 B 量出来的就是这种父子装配不一致（181,040 对 76,896 个字符）。不会报错，只会贵。

续接：

```python
resume_from: History | None = None
try:
    previous_path = resolve("last", thread_dir)
except RolloutError:
    pass  # first turn in this thread
else:
    loaded = read_rollout(previous_path)
    resume_from, dropped = loaded.history()
    if loaded.truncated_at is not None:
        announce(
            f"session file damaged from line {loaded.truncated_at}; using what precedes it"
        )
    if dropped:
        resume_from.add_system_note(interrupted_note(dropped))
        announce(f"resumed: {dropped} incomplete message(s) discarded")
    note = environment_note(loaded.meta, current)
    if note is not None:
        resume_from.add_system_note(note)
        announce("environment changed since this session was recorded")
    current = replace(current, forked_from=loaded.meta.session_id)

```

> - `resolve("last", thread_dir)`：**这个会话目录里**最后一个 rollout 文件（§2.3）。第一轮时目录是空的，抛 `RolloutError`，直接跳过。
> - 读回来的历史可能有损坏、可能有被打断的半截，各自加一条系统提示并告诉浏览器——和命令行的 `--resume` 一样。
> - `forked_from`：新文件记下它是从哪个文件接着来的。

写入器、录制、三个 watcher、派生事件、组装 Agent：

```python
writer = StreamingRolloutWriter(rollout_path(current.session_id, thread_dir), current, emit)
recorder.record(
    "config",
    {
        "provider": provider_name,
        "model": model,
        "cwd": str(root),
        "sandbox_mode": session.mode,
        "approval_policy": session.policy,
        "context_window": context_window,
        "question": question,
        "tools": sorted(tools.handlers),
        "schemas": tools.schemas,
        "memory": str(memory.directory) if memory else None,
        "skills": str(skills.directory) if skills else None,
    },
)
# Every message in a browser thread is a resume (the thread is reloaded
# from its rollout file), so both watchers are told what the conversation
# already holds. Without it, each follow-up message added another copy of
# AGENTS.md and of the memory block -- chapter 13's resume fix and chapter
# 16's, which reached `__main__` and not this file. Found when chapter 16
# was rewritten.
shown = resume_from.developer_notes() if resume_from is not None else ()
agents_watcher = AgentsMdWatcher(root, shown=shown)
# Three watchers, one hook -- `__main__._ask`'s arrangement, for its
# reasons. `Wiring.agent(preamble=...)` used to carry the memory and skill
# blocks here; that parameter is gone (F16-11), and what replaced it is the
# per-turn hook chapter 13 already had, with each watcher deciding for
# itself whether it has anything to say this turn.
memory_watcher = MemoryWatcher(memory, root=root, shown=shown)
# Always built, for the reason `memory_watcher` is: a resumed session may
# hold a catalog an earlier process delivered (chapter 18's rewrite).
skills_watcher = SkillsWatcher(skills, shown=shown)

def _on_turn_start() -> str | None:
    notes = [
        agents_watcher.refresh(Path(context.shell.cwd)),
        memory_watcher.refresh(),
        skills_watcher.refresh(),
    ]
    said = [note for note in notes if note is not None]
    return "\n\n".join(said) if said else None

# Two things the browser must see that are not history items: the plan, and
# the sandbox mode `request_permissions` can rewrite mid-turn. Derived by
# comparing against the last value after every item is durable, rather than
# by a callback the core would have to grow. It costs one tuple comparison
# per item and it means neither feature needed a hook.
writer.after_item = _derived(task_plan, session, emit)

agent = wiring.agent(
    llm,
    tools,
    instructions=_instructions(session, tools, memory=memory, skills=skills),
    rollout=writer,
    resume_from=resume_from,
    on_stop=unfinished_note(task_plan),
    on_turn_start=_on_turn_start,
)

```

> - `StreamingRolloutWriter(...)`：每一轮一个新文件，路径来自第 7 章的 `rollout_path`。
> - `shown = ...`：续接时把旧对话里已有的 developer 消息交给 watcher，免得重复（第 16、18 章改写时修的，注释里有）。
> - `writer.after_item = _derived(...)`：每写完一条，看看计划和权限有没有变（本节最后）。

后台写记忆、跑、收尾：

```python
writer_task: asyncio.Task[Any] | None = None
if remember is not None:
    writer_task = asyncio.ensure_future(
        run_pipeline(
            make_model([]),
            directory=remember,
            sessions_dir=thread_dir,
            # Global, like the memory it books work against (F17-12): one
            # queue per machine, not one per workspace the server hosts.
            jobs_path=DEFAULT_JOBS_PATH,
            exclude=(current.session_id,),
            headroom=lambda: llm.rate_limit.headroom() if llm.rate_limit else None,
        )
    )

try:
    result = await agent.run(question)
except ModelFailed as exc:
    emit({"type": "error", "text": str(exc)})
    if writer_task is not None:
        announce(await _finish_writer(writer_task, grace=0.0))
    raise
except asyncio.CancelledError:
    # The stop button. `agent.run` writes `mark("interrupted")` on its way
    # out, so the rollout already says where it stopped and the next turn
    # in this thread resumes from there.
    if writer_task is not None:
        writer_task.cancel()
    emit({"type": "notice", "text": "stopped"})
    raise
finally:
    for client in clients:
        await client.close()
    writer.release()

```

> - **后台写记忆的任务在 `agent.run` 之前就启动**（第 17 章 F17-01）：放在这一轮结束之后，实测是答案出来到能再输入之间多等 14 秒。它处理的是**之前**几轮的会话文件，这一轮的被 `exclude` 排除了。
> - `make_model([])`：**空的工具表**。写记忆是两次不需要工具的模型调用，把这一轮的工具描述给它，每次请求都白付那些 token。
> - **`except ModelFailed`**：模型那边彻底失败了（第 12 章的重试都用完了），发一个 `error` 事件，再往上抛。
> - **`except asyncio.CancelledError`**：用户点了"停止"（§3.2）。
> - **`finally`**：不管怎样，关掉 MCP 连接、释放 rollout 文件的锁（第 7 章）。

报告和最后一个事件：

```python
answer, cited = result.final_text, None
if memory:
    cited = parse_citations(result.final_text, memory)
    answer = cited.text
    record_uses(memory.directory, cited.used)

reports = [
    f"{llm.model} | {result.stop_reason} after {result.turns_used} turn(s)",
    task_plan.describe(),
    session.describe(),
]
if memory:
    reports.append(memory.describe())
    assert cited is not None
    reports.append(cited.describe())
if writer_task is not None:
    reports.append(await _finish_writer(writer_task))
reports.extend(event.describe() for event in result.compactions)
reports.append(f"tokens: {agent.calibration.describe()}")
reports.extend(line.strip("[]") for line in describe_children(sub_ctx.children))

emit(
    {
        "type": "turn_complete",
        "final_text": answer,
        "stop_reason": result.stop_reason,
        "turns_used": result.turns_used,
        "model": llm.model,
        "plan": _plan_payload(task_plan),
        "reports": reports,
        "transcript": str(recorder.path),
        "session_file": str(writer.path),
        "citations": list(cited.used) if cited is not None else [],
    }
)
return result

```

> 命令行在答案下面打印的那几行小字（多少轮、计划、权限、记忆、token……），这里收集成 `reports` 列表，放进 `turn_complete` 事件。

最后三个小函数：

```python
def _plan_payload(plan: TaskPlan) -> list[dict[str, str]]:
    return [{"text": step.text, "status": step.status} for step in plan.steps]


def _derived(plan: TaskPlan, session: Session, emit: EventSink) -> Callable[[], None]:
    """Emit the plan and the permission state, but only when they change."""
    last: dict[str, Any] = {"plan": _plan_payload(plan), "mode": session.mode}

    def check() -> None:
        current_plan = _plan_payload(plan)
        if current_plan != last["plan"]:
            last["plan"] = current_plan
            emit({"type": "plan_updated", "steps": current_plan, "revisions": plan.updates})
        if session.mode != last["mode"]:
            previous, last["mode"] = last["mode"], session.mode
            emit(
                {
                    "type": "session_mode_changed",
                    "from": previous,
                    "mode": session.mode,
                    "policy": session.policy,
                }
            )

    return check


def resolve_api_key(provider: dict[str, Any]) -> str | None:
    """The pasted key, or the environment, never a file.

    A key typed into the browser is kept in `providers.json` because there is
    nowhere else for it to live; a key that came from `OPENAI_API_KEY` is read
    at call time and never written down, so clearing the form in the browser
    cannot copy the environment's key into the file.
    """
    if provider.get("api_key"):
        return str(provider["api_key"])
    if provider.get("provider") == "openai":
        return os.environ.get("OPENAI_API_KEY")
    return None
```

> - **`_derived`**：一个闭包，记着"上次的计划"和"上次的沙箱模式"。每写完一条历史，比一下，变了就发事件。为什么不给 `TaskPlan` 加回调，§15 的 R2 讲了：那样就得改 Agent 的代码。
> - **`resolve_api_key`**：浏览器里填的 key 存在文件里；没填就从环境变量读，**读了也不写进文件**。

两个测试守着"控制台只是 Agent 的一个用户，不是它的一份拷贝"：

```python
def test_the_console_never_constructs_its_own_agent() -> None:
    """Interlude B's rule, which the console is the first new caller to test:
    a second construction site is how a child ended up without a recorder,
    without compaction and without the scheduler, silently, for a chapter."""
    checker = _load_checker()
    assert checker.check_one_agent_construction(SRC) == []


async def test_the_console_reports_plan_and_permission_changes_without_a_core_hook() -> None:
    """The plan and the sandbox mode are not history items, so nothing writes
    them to the rollout -- but the browser has to see both: a plan that only
    appears when the turn ends is not a plan you can follow, and a mid-turn
    `request_permissions` escalation that shows up nowhere is the console
    lying about what the agent may currently do.

    Derived by comparing against the last value after each item is durable,
    rather than by a callback added to `agent.py`. That is the whole reason
    `probe_web.py core-diff` can report zero.
    """
    from minicodex.plan import PlanStep, TaskPlan
    from minicodex.web.runtime import _derived

    events: list[dict[str, Any]] = []
    plan, session = TaskPlan(), Session(mode="read-only", policy="on-request")
    check = _derived(plan, session, events.append)

    check()
    assert events == [], "nothing changed, so nothing is said"

    plan.steps = (PlanStep("write it", "in_progress"),)
    check()
    assert events[-1]["type"] == "plan_updated"
    assert events[-1]["steps"] == [{"text": "write it", "status": "in_progress"}]

    session.mode = "workspace-write"  # what `request_upgrade` does, in place
    check()
    assert events[-1] == {
        "type": "session_mode_changed",
        "from": "read-only",
        "mode": "workspace-write",
        "policy": "on-request",
    }

    check()
    assert len(events) == 2, "an unchanged plan must not re-announce itself every item"
```

### 2.7 改写时发现：第 17 章的修复，第三次没走到 `web/`

第 17 章改写时发现 `__main__._finish_writer` 有两个漏洞：只接住四种异常（可后台写记忆时模型出错，抛出来的是客户端自己的 `ModelHTTPError` 或 `httpx` 的错误，不在那四种里）；而且在"宽限期等待"那一步，`wait_for` 会把任务的异常直接抛出来，根本走不到下面的 `try`。那次修了 `__main__`。

`web/runtime.py` 里那份复制没人动。对照一下第一版：

```python
    if not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), grace)
        except asyncio.TimeoutError:
            task.cancel()
            return f"memory writer: still going after {grace:.0f}s, left for the next run"
    try:
        return (await task).describe()
    except asyncio.CancelledError:
        return "memory writer: stopped, left for the next run"
    except (MemoryWriteError, MemoryError_, OSError, ModelFailed) as exc:
        return f"memory writer: failed, nothing written ({type(exc).__name__}: {exc})"
```

在控制台里，后台写记忆的任务一出错，异常就从 `run_turn` 里冒出去——这一轮明明已经答完了，浏览器上却显示一条 `error`，后面的报告全丢了。现在的版本（上面 §2.6 那段）在两个地方都接 `Exception`。

```python
async def test_a_failing_memory_writer_does_not_fail_the_turn() -> None:
    """Chapter 17's rewrite fixed `__main__._finish_writer`; the console's copy
    kept the four exception types, so a provider error in the background writer
    reached the browser as the turn's error."""
    from minicodex.web.runtime import _finish_writer

    class ProviderSaidNo(Exception):
        pass

    async def failing() -> Any:
        raise ProviderSaidNo("HTTP 500")

    done = asyncio.ensure_future(failing())
    await asyncio.sleep(0)
    assert "failed" in await _finish_writer(done)

    slow = asyncio.ensure_future(asyncio.sleep(0.01, result=None))

    async def fails_later() -> Any:
        await slow
        raise ProviderSaidNo("HTTP 500")

    assert "failed" in await _finish_writer(asyncio.ensure_future(fails_later()), grace=5.0)
```

> 两种情况各测一次：任务已经失败了；任务在宽限期里才失败。

**这是同一个形状的第三次。** 第 16 章改写时，`MemoryWatcher` 的续接修复到了 `__main__`、没到 `web/runtime.py`；第 18 章改写时，`SkillsWatcher` 的又是一样；这一次是 `_finish_writer`。三次都是：**修一个 bug 时，只修了眼前那一份，没去找它的复制品。** `web/runtime.py` 是 `__main__._ask` 的同构，这件事写在它的第一行说明里——下一次修 `_ask` 里的任何东西，都应该顺手打开这个文件。

---

## §3 F19-01：两条消息一起到，起了两个 turn


第一版的发消息路由长这样（第一版的写法，后来改掉了；`run_turn(...)` 的参数这里省略了）：

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
console.busy.add(thread_id)
```

> 判断和占位之间没有任何 `await`——事件循环在这两行之间不可能切去跑别的请求，所以第二个请求一定看得到第一个的占位。

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


测试文件 `tests/test_faults_ch19.py` 的开头。说明把这一章的故障分成两类——控制台里的，和**检查**里的（§8）：

```python
"""Chapter 19: the web console. Every test here is offline.

Nothing in this file opens a socket to anything but itself. The agent runs
against `stub_url` -- the recorded-Ollama server every other chapter uses -- and
the console runs through `TestClient`, which speaks ASGI directly. The one
question that needs a real model, *what a reader waits for when the console
streams per history item rather than per token*, is measured by
`probe_web.py stream` against the real API and is not asserted here.

The faults below divide into two kinds, and the split is the chapter's point.
F19-01 through F19-05 and F19-09 through F19-11 are faults in the console: a
race, a thread, an ordering, a leak, a missing reconnect, a dropped event, an
unvalidated path, a default that is not a consent. F19-06 through F19-08 are
faults in the *checks* -- rules that had been passing for eighteen chapters and
silently stopped covering anything the moment the package grew a subdirectory.
The second kind is the more expensive one, because a green tick kept saying
otherwise.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from minicodex.approval import ApprovalReply, ApprovalRequest, Session
from minicodex.policy import APPROVAL_POLICIES, SANDBOX_MODES, Risk
from minicodex.web import create_app
from minicodex.web.approver import ApprovalBroker, WebApprover
from minicodex.web.channel import Channel
from minicodex.web.store import DEFAULT_THREAD_SETTINGS, SEED_PROVIDERS, Store, validate_settings

SRC = Path(__file__).resolve().parent.parent / "src" / "minicodex"
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
```

> `SRC` 和 `FRONTEND`：从这个测试文件的位置推出源代码和前端目录，几个测试要直接读源文件（§7.11、§9）。

现在的两个夹具（fixture，pytest 里给测试准备环境的函数，测试参数里写同名就会自动调用）：

```python
@pytest.fixture
def console(tmp_path: Path) -> Any:
    # As a context manager, so the app's lifespan runs and one event loop
    # survives between requests. Without that, a background turn is finished
    # before the next request arrives and "is this thread busy" is never true
    # -- which would make F19-01's tests pass against the broken code too.
    with TestClient(create_app(tmp_path / "console")) as client:
        yield client


@pytest.fixture
def hanging_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `run_turn` with one that never finishes.

    The race in F19-01 is about *when* the claim is written relative to the
    response, so the test needs a turn that is still running when the second
    request arrives. A real one against the stub is over in milliseconds; this
    one is over when the test is.
    """
    from minicodex.web import routes

    async def never(**_kwargs: Any) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(routes, "run_turn", never)
```

> - `with TestClient(...) as client: yield client`：`yield` 之前是准备，之后是收尾。用 `with` 才会运行应用的启动和关闭钩子，并且让同一个事件循环活过多个请求。
> - `monkeypatch.setattr(routes, "run_turn", never)`：在这个测试里把 `routes` 模块里的 `run_turn` 换成一个睡一小时的假函数。

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


### 3.2 代码：发消息、停止

`routes.py` 的开头。说明讲了这个文件的定位（薄薄一层），以及唯一两处"承重"的地方：

```python
"""The console's HTTP and WebSocket surface.

Deliberately thin. Everything that knows about `minicodex` is in runtime.py,
everything that knows about JSON files is in store.py, and everything that
knows about socket ordering is in channel.py. What is left here is turning a
request into a call on one of those three and its result into JSON.

Two things in this file are load-bearing rather than plumbing, and both are
about a turn being a *background* task that outlives its request:

`_busy` is claimed **before** `create_task`, not inside the coroutine. The
first version tested membership in the handler and added inside `go()`, which
does not start until the handler has already returned -- so two requests
arriving together both passed the check and started two runs on one thread
directory, with two `RolloutWriter`s racing for the same lock and
`resolve("last", ...)` picking whichever landed first.

`_running` keeps the task object, which is what makes a stop button possible at
all, and is also the strong reference `asyncio` does not keep for you.
"""

from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from minicodex.approval import ApprovalReply
from minicodex.memory import BODY_FILE, SUMMARY_FILE, usage
from minicodex.memory import load as load_memory
from minicodex.memory_jobs import JobStore
from minicodex.policy import APPROVAL_POLICIES, SANDBOX_MODES, policy_tables
from minicodex.rollout import RolloutError, _dump_item, fork, list_sessions, read_rollout, resolve
from minicodex.rules import RuleStore
from minicodex.skills import discover as discover_skills
from minicodex.tools import TOOL_SCHEMAS

from .runtime import resolve_api_key, run_turn
from .store import DEFAULT_THREAD_SETTINGS, validate_settings

router = APIRouter()


def _console(request: Request) -> Any:
    """The one `Console` this app was built with. See app.py."""
    return request.app.state.console
```

> - `router = APIRouter()`：一组路由。`app.py` 里 `include_router(router)` 把它们全挂上去。
> - `_console(request)`：从请求找到这个进程的 `Console`（§2.5）。

发消息：

```python
class SendMessage(BaseModel):
    text: str


@router.post("/api/threads/{thread_id}/messages", status_code=202)
async def send_message(request: Request, thread_id: str, body: SendMessage) -> dict[str, Any]:
    console = _console(request)
    record = console.require_thread(thread_id)
    if not body.text.strip():
        raise HTTPException(400, "message cannot be empty")

    # Claimed here, synchronously, before anything can await. See this module's
    # docstring: doing it inside the task is the race.
    if thread_id in console.busy:
        raise HTTPException(409, "this thread is still answering the previous message")
    console.busy.add(thread_id)

    provider = console.store.active_provider()
    if provider is None:
        console.busy.discard(thread_id)
        raise HTTPException(400, "no active model provider -- add one under Extensions")

    servers = [s for s in console.store.all("mcp.json") if s.get("enabled", True)]
    emit = console.channel.emitter(thread_id)
    emit({"type": "turn_started", "text": body.text})

    async def go() -> None:
        try:
            await run_turn(
                thread_dir=console.store.thread_dir(thread_id),
                workspace_root=Path(record["workspace"]),
                question=body.text,
                settings=record["settings"],
                provider_name=provider["provider"],
                base_url=provider["base_url"],
                model=provider["model"],
                api_key=resolve_api_key(provider),
                mcp_servers=servers,
                broker=console.broker,
                emit=emit,
            )
        except asyncio.CancelledError:
            pass  # the stop button; run_turn already said so and marked the file
        except Exception as exc:
            # `ModelFailed` has already emitted its own, more specific line from
            # inside run_turn. This is the catch-all for everything else -- a
            # bad MCP command, a permission error on the workspace -- so that a
            # thread never simply goes quiet on the browser.
            emit({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            console.busy.discard(thread_id)
            console.running.pop(thread_id, None)
            emit({"type": "turn_finished"})

    console.running[thread_id] = asyncio.get_running_loop().create_task(go())
    return {"accepted": True}
```

> - `status_code=202`：成功时回 202 "收到了，还没做完"——这一轮是在后台跑的，响应回去的时候它才刚排上队。
> - 先检查文字不为空，再**同步地**占位，再找当前启用的提供方——找不到要记得把占位放掉（`busy.discard`），不然这个会话就永远"忙"了。
> - `emit = console.channel.emitter(thread_id)`：一个只发给这个会话的 `emit` 函数（§6.1）。
> - **`go()`**：真正在后台跑的协程。`run_turn` 的每一个参数都从存储里来。三种结束方式：被取消（停止按钮）就安静地结束；其他任何异常都变成一个 `error` 事件——浏览器上不能"什么都不说就停了"；不管怎样，`finally` 放掉占位、移除任务、发 `turn_finished`。
> - `console.running[thread_id] = ...create_task(go())`：把任务存起来——停止按钮要用它，而且 asyncio 自己只持有任务的弱引用（§6）。

停止：

```python
@router.post("/api/threads/{thread_id}/cancel")
async def cancel_turn(request: Request, thread_id: str) -> dict[str, bool]:
    console = _console(request)
    console.require_thread(thread_id)
    task = console.running.get(thread_id)
    if task is None or task.done():
        raise HTTPException(409, "this thread is not answering anything")
    task.cancel()
    return {"ok": True}
```

> `task.cancel()` 会在 `go()` 正在 `await` 的那个地方抛出 `CancelledError`，一路传到 `agent.run` 里面；Agent 退出时写一个 `interrupted` 标记，下一条消息从那里接着来。

测试用到的两个辅助：

```python
@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def _thread(client: TestClient, workspace: Path, **settings: Any) -> str:
    response = client.post(
        "/api/threads",
        json={"workspace": str(workspace), "title": "test", "settings": settings},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]
```

三个测试：

```python
def test_F19_01_the_busy_flag_is_claimed_before_the_task_is_created(
    console: Any, workspace: Path, hanging_turn: None
) -> None:
    """Two messages arriving together must not start two runs on one thread.

    The original tested `thread_id in _busy` in the handler and did `_busy.add`
    inside the coroutine passed to `create_task` -- which does not begin
    running until the handler has returned. Both requests therefore passed the
    check, and two `RolloutWriter`s opened on the same directory.

    Asserted through the app rather than on the set, because the bug is *when*
    the set is written relative to the response, and only a request can see
    that ordering.
    """
    thread_id = _thread(console, workspace)
    provider = console.get("/api/providers").json()[0]
    console.post(f"/api/providers/{provider['id']}/activate")

    first = console.post(f"/api/threads/{thread_id}/messages", json={"text": "one"})
    second = console.post(f"/api/threads/{thread_id}/messages", json={"text": "two"})

    assert first.status_code == 202
    assert second.status_code == 409
    assert "still answering" in second.json()["detail"]


def test_F19_01_a_rejected_second_message_does_not_release_the_first(
    console: Any, workspace: Path, hanging_turn: None
) -> None:
    """The 409 path must not run the `finally` that clears the claim."""
    thread_id = _thread(console, workspace)
    provider = console.get("/api/providers").json()[0]
    console.post(f"/api/providers/{provider['id']}/activate")
    console.post(f"/api/threads/{thread_id}/messages", json={"text": "one"})
    console.post(f"/api/threads/{thread_id}/messages", json={"text": "two"})
    # Still claimed by the first, which is what makes the third a 409 too.
    third = console.post(f"/api/threads/{thread_id}/messages", json={"text": "three"})
    assert third.status_code == 409


def test_F19_01_settings_cannot_change_underneath_a_running_turn(
    console: Any, workspace: Path, hanging_turn: None
) -> None:
    """`run_turn` reads the settings once, at the top. Editing them mid-turn
    would silently apply to the *next* one while the UI showed the new value as
    though it were in force now."""
    thread_id = _thread(console, workspace)
    provider = console.get("/api/providers").json()[0]
    console.post(f"/api/providers/{provider['id']}/activate")
    console.post(f"/api/threads/{thread_id}/messages", json={"text": "one"})
    response = console.patch(
        f"/api/threads/{thread_id}", json={"settings": {"sandbox_mode": "full-access"}}
    )
    assert response.status_code == 409
```

> - 第二个：被拒绝的第二条消息**不能**顺手把第一条的占位放掉——否则第三条就能进来。
> - 第三个：一轮正在跑的时候不能改设置（`PATCH` 回 409）。`run_turn` 只在开头读一次设置；中途改了，界面上显示的是新值，这一轮用的却还是旧值。

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
async def test_F19_02_the_broker_resolves_a_future_from_another_thread() -> None:
    """`asyncio.Future` is not thread-safe, and a `def` FastAPI route runs in a
    worker thread. Calling `set_result` from there is a data race that the loop
    usually wins -- so the failure is rare, unreproducible, and looks like a
    turn that hung until the 600-second approval timeout.

    The route is `async def` now, and the broker goes through
    `call_soon_threadsafe` regardless, which is what this pins: resolving from
    a thread that is not the loop's still delivers exactly one result.
    """
    broker = ApprovalBroker()
    future = broker.register("abc")

    reply = ApprovalReply(True, "ls")
    done = await asyncio.get_running_loop().run_in_executor(
        None, lambda: broker.resolve("abc", reply)
    )
    assert done is True
    assert await asyncio.wait_for(future, 2.0) == reply


async def test_F19_02_a_second_answer_to_the_same_approval_is_refused() -> None:
    """Two clicks, or a click and a retry, must not resolve a future twice --
    `set_result` on a done future raises `InvalidStateError` inside whatever
    was unlucky enough to call it."""
    broker = ApprovalBroker()
    broker.register("abc")
    assert broker.resolve("abc", ApprovalReply(True, "ls")) is True
    assert broker.resolve("abc", ApprovalReply(False, "ls")) is False


async def test_F19_02_an_approval_that_is_never_answered_times_out_as_a_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing closed, and bounded. An approval nobody answers must not hold
    the turn open for ever (F02-01, one layer up)."""
    from minicodex.web import approver as approver_module

    monkeypatch.setattr(approver_module, "APPROVAL_TIMEOUT_SECONDS", 0.05)
    events: list[dict[str, Any]] = []
    web_approver = WebApprover(ApprovalBroker(), events.append)

    reply = await web_approver.ask(
        ApprovalRequest(what="rm -rf /", reason="because", risk=Risk.WRITE, suggested_rule=None)
    )
    assert reply.approved is False
    assert [e["type"] for e in events] == ["approval_request", "approval_timeout"]


async def test_F19_02_a_reply_that_lost_the_race_with_the_timeout_is_refused() -> None:
    """The half of `resolve`'s guard that `pop` was hiding.

    `test_..._a_second_answer_is_refused` above looks like it covers
    `if future is None or future.done()`. It does not: `resolve` *pops*, so the
    second call never finds a future at all and returns on the first half of
    that `or`. Deleting `or future.done()` left it green -- caught by
    `probe_mutations_ch19.py`, not by reading it.

    The case the second half is actually for is narrower and real. When
    `asyncio.wait_for` gives up it **cancels** the future, and `ask` only then
    calls `discard`. A reply that arrives in that window finds a future which is
    still in the table and already done, and `set_result` on it raises
    `InvalidStateError` -- inside the HTTP route, for a click the person was
    entitled to make.
    """
    broker = ApprovalBroker()
    future = broker.register("abc")
    future.cancel()  # exactly what `wait_for` does when the approval times out

    assert broker.resolve("abc", ApprovalReply(True, "ls")) is False


async def test_F19_02_a_reply_from_a_worker_thread_goes_through_the_loop() -> None:
    """The thread hop this module's docstring promises, asserted as a mechanism.

    The assertion is on *how* the future was resolved rather than on the reply,
    and that is deliberate rather than lazy. `future.set_result` called from an
    anyio worker thread is a data race, and a data race is precisely the thing
    that does not fail when you test it: it returns the right answer here every
    time, and loses in production once. So there is nothing to observe in the
    outcome -- the only honest evidence is that the call was handed to the loop
    instead of made on the spot.

    `future.set_result in scheduled` rather than `scheduled` being non-empty:
    `asyncio.to_thread` uses `call_soon_threadsafe` for its own bookkeeping, so
    a spy that only counts calls stays green with the hop deleted.
    """
    broker = ApprovalBroker()
    future = broker.register("abc")
    loop = asyncio.get_running_loop()

    scheduled: list[Any] = []
    original = loop.call_soon_threadsafe

    def spy(callback: Any, *args: Any) -> Any:
        scheduled.append(callback)
        return original(callback, *args)

    loop.call_soon_threadsafe = spy  # type: ignore[method-assign]
    try:
        assert await asyncio.to_thread(broker.resolve, "abc", ApprovalReply(True, "ls")) is True
        assert (await future).approved is True
    finally:
        loop.call_soon_threadsafe = original  # type: ignore[method-assign]

    assert future.set_result in scheduled, (
        "a reply answered on a worker thread must reach the future through "
        "loop.call_soon_threadsafe, not by touching it directly"
    )
```

> - 第一个：`run_in_executor(None, ...)` 把一个函数丢到线程池里跑——模拟"一个 `def` 路由在别的线程里调用 `resolve`"——然后检查等待方真的被唤醒了。
> - 第三个：`monkeypatch` 把 600 秒的超时改成很短，没人回答就当作拒绝。
> - 最后一个断言的是**机制**而不是结果（§3.1 末尾那段"写在后面"讲过为什么）：在 `call_soon_threadsafe` 外面包一层记录器，看 `future.set_result` 是不是真的被交给了事件循环。

顺手还有一条：`asyncio.get_event_loop()` 从 3.10 起就废弃了，
没有运行中的循环时它会**新建一个**——那样 future 会属于一个永远不会跑的循环。
本章统一用 `get_running_loop()`。


### 4.1 代码：`approver.py`

说明和常量：

```python
"""The browser's `Approver`, and the table of what it is currently waiting on.

`minicodex.approval.Approver` is a Protocol with exactly one method:

    async def ask(self, request: ApprovalRequest) -> ApprovalReply: ...

The CLI's implementation blocks on `input()`. This one blocks on an
`asyncio.Future` that a later HTTP request resolves. Nothing in
`minicodex.approval`, `minicodex.policy` or `minicodex.agent` changed to make
that possible -- chapter 5 wrote the seam for a reason that had nothing to do
with browsers, and the tool call really is suspended mid-turn either way.

The subtlety is which thread resolves the future. `asyncio.Future` is not
thread-safe, and a FastAPI route declared `def` rather than `async def` runs in
an anyio worker thread -- so `future.set_result(...)` from a plain `def` route
is a data race against the event loop, on every reply. It does not usually
lose; it loses under load, once, and the turn hangs until the timeout. Two
defences, because one of them is a convention and conventions travel badly:
the route is `async def` (see `routes.py`), and `resolve` goes through
`loop.call_soon_threadsafe` regardless of who calls it.

The broker also keeps *what* it is waiting on, per thread, and that arrived
when chapter 19 was rewritten. The request reaches the browser as one event;
an event sent while no tab was watching that thread is dropped (channel.py),
and a tab that reconnects rebuilds itself from `GET /api/threads/{id}`. The
first version's reply to that request had no approvals in it, so a reload, a
dropped socket or switching to another thread and back made the card vanish
while the turn stayed suspended on it -- for `APPROVAL_TIMEOUT_SECONDS`, after
which it counted as a refusal nobody had made.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from minicodex.approval import ApprovalReply, ApprovalRequest

EventSink = Callable[[dict[str, Any]], None]

#: How long a pending approval waits before the turn gives up and treats it as
#: a decline. Long enough that a person who looks away does not lose the turn;
#: bounded, because a turn that can wait forever is a hang, and chapter 2 (
#: F02-01) already established that an unbounded wait is not a safe default
#: just because the thing being waited on is a person.
APPROVAL_TIMEOUT_SECONDS = 600.0
```

> 说明的最后一段是这次改写加的，§4.3 讲。

等待表 `ApprovalBroker`：

```python
class ApprovalBroker:
    """Every approval this process is currently suspended on."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ApprovalReply]] = {}
        #: The `approval_request` event for each pending id, and which thread asked.
        self._asked: dict[str, tuple[str | None, dict[str, Any]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def register(
        self, approval_id: str, *, thread: str | None = None, event: dict[str, Any] | None = None
    ) -> asyncio.Future[ApprovalReply]:
        # `get_running_loop`, not `get_event_loop`: the latter is deprecated
        # from 3.10 and, when there is no running loop, creates one -- so the
        # future would belong to a loop nothing ever runs.
        loop = asyncio.get_running_loop()
        self._loop = loop
        future: asyncio.Future[ApprovalReply] = loop.create_future()
        self._pending[approval_id] = future
        if event is not None:
            self._asked[approval_id] = (thread, event)
        return future

    def pending(self) -> list[str]:
        return [key for key, fut in self._pending.items() if not fut.done()]

    def asked(self, thread: str) -> list[dict[str, Any]]:
        """The requests `thread` is still waiting on an answer for."""
        return [
            event
            for approval_id, (who, event) in self._asked.items()
            if who == thread
            and approval_id in self._pending
            and not self._pending[approval_id].done()
        ]

    def discard(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)
        self._asked.pop(approval_id, None)

    def resolve(self, approval_id: str, reply: ApprovalReply) -> bool:
        self._asked.pop(approval_id, None)
        future = self._pending.pop(approval_id, None)
        if future is None or future.done():
            return False
        loop = self._loop
        if loop is None or loop is _running_loop():
            future.set_result(reply)
        else:
            loop.call_soon_threadsafe(future.set_result, reply)
        return True

    def fail_all(self, reason: str) -> None:
        """Decline everything outstanding, e.g. when the turn is cancelled.

        A cancelled turn must not leave a future nobody will ever resolve: the
        `ask` coroutine is inside the tool call being cancelled, so the
        cancellation lands there anyway -- this is for the entries whose
        coroutine is already gone, so the table does not grow.
        """
        self._asked.clear()
        for approval_id in list(self._pending):
            future = self._pending.pop(approval_id, None)
            if future is not None and not future.done():
                future.set_exception(RuntimeError(reason))
```

> - **`_pending`**：审批 id → 正在等的 Future。**`_asked`**：审批 id →（哪个会话问的，发给浏览器的那个事件）。后者是改写时加的。
> - **`register`**：在**正在运行的**事件循环上造一个 Future，记下来，顺便记住是哪个循环。
> - **`pending` / `asked`**：还在等的那些。`asked(thread)` 只返回这个会话的，给 `GET /api/threads/{id}` 用。
> - **`resolve`**：从表里拿走这个 Future（`pop`——所以第二次回答找不到它，返回 `False`）；已经结束了（比如超时刚取消掉）也返回 `False`；否则放进结果。在不在事件循环的线程里，决定是直接放还是请循环替你放（`call_soon_threadsafe`）。
> - **`fail_all`**：服务器关闭时，所有还在等的都以异常结束，表清空。

一个小工具函数：没有正在运行的循环时，`get_running_loop` 会抛异常，这里把它变成 `None`：

```python
def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
```

`WebApprover`：

```python
class WebApprover:
    """Suspend the tool call, ask the browser, resume with what it said."""

    def __init__(self, broker: ApprovalBroker, emit: EventSink, thread: str | None = None) -> None:
        self._broker = broker
        self._emit = emit
        #: Which thread is asking, so a reconnecting tab can be told (see the
        #: module docstring). `None` for a broker nobody will ever query.
        self._thread = thread

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        approval_id = uuid.uuid4().hex[:12]
        event = {
            "type": "approval_request",
            "id": approval_id,
            "what": request.what,
            "reason": request.reason,
            "risk": request.risk.name,
            "suggested_rule": (
                list(request.suggested_rule) if request.suggested_rule is not None else None
            ),
        }
        future = self._broker.register(approval_id, thread=self._thread, event=event)
        self._emit(event)
        try:
            reply = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # See channel.py: the builtin spelling does not fire on 3.10.
            self._broker.discard(approval_id)
            self._emit({"type": "approval_timeout", "id": approval_id})
            return ApprovalReply(False, request.what)
        except asyncio.CancelledError:
            self._broker.discard(approval_id)
            raise
        self._emit(
            {
                "type": "approval_resolved",
                "id": approval_id,
                "approved": reply.approved,
                "remember": reply.remember,
            }
        )
        return reply
```

> - `thread`：哪个会话在问（§4.3）。
> - `try: reply = await asyncio.wait_for(...)` 三种结局：超时（从表里删掉，告诉浏览器"超时了"，当作拒绝）；被取消（这一轮被停止了，删掉，继续往上抛）；正常拿到回答（告诉浏览器"已经回答了"，把回答交回第 5 章的审批流程）。
> - `ApprovalReply(False, request.what)`：拒绝，命令原样。

### 4.2 代码：回答审批的路由

```python
class ApprovalResponse(BaseModel):
    approved: bool
    command: str | None = None
    remember: str | None = None  # "session" | "project" | None


@router.post("/api/approvals/{approval_id}/respond")
async def respond_to_approval(
    request: Request, approval_id: str, body: ApprovalResponse
) -> dict[str, bool]:
    """`async def` on purpose -- see approver.py.

    A `def` route runs in an anyio worker thread, and resolving an
    `asyncio.Future` from a thread that is not the loop's is a race the loop
    usually wins and occasionally does not. The broker also goes through
    `call_soon_threadsafe`, but the route being `async` is what makes that the
    belt rather than the braces.
    """
    console = _console(request)
    if body.remember not in (None, "session", "project"):
        raise HTTPException(400, f"unknown remember scope {body.remember!r}")
    reply = ApprovalReply(
        approved=body.approved,
        command=body.command or "",
        remember=body.remember,  # type: ignore[arg-type]
    )
    if not console.broker.resolve(approval_id, reply):
        raise HTTPException(404, "no pending approval with that id (it may have timed out)")
    return {"ok": True}
```

> - `remember`：`None`（只这一次）、`"session"`（这次运行里都允许）、`"project"`（存到工作区的规则文件里，以后都允许）。第 5 章的三种。不认识的值直接 400。
> - `body.command or ""`：浏览器可以**改**命令再批准（§15 的 R4）。
> - `resolve` 返回 `False`（找不到、已经超时）就回 404，浏览器上那张卡片会显示"发送失败"（§7.4）。

### 4.3 改写时发现：刷新一下，那张卡片就没了

审批请求是作为**一个事件**发给浏览器的。§6 会讲：没人在看这个会话的时候，事件直接丢掉；浏览器重连或刷新之后，用 `GET /api/threads/{id}` 重建页面。

第一版的 `GET /api/threads/{id}` 返回的是历史记录、标记、忙不忙——**没有正在等的审批**。于是：

- 刷新一下页面；
- 或者切到另一个会话再切回来；
- 或者网络断了一下又重连上；

那张审批卡片就消失了。而服务器上，那一轮还停在 `await future` 上。用户看到的是一个"正在思考"的会话，什么按钮都没有；10 分钟后，超时把它当成一次拒绝——一次谁都没做过的拒绝。

修法分两半。后端：`ApprovalBroker` 记住每个审批请求的事件，以及是哪个会话问的（上面 §4.1 的 `_asked`、`asked`）；`run_turn` 造 `WebApprover` 时告诉它会话目录（§2.6）；`get_thread` 把这个会话还在等的审批放进返回值：

```python
return {
    **record,
    "items": items,
    "marks": marks,
    "busy": thread_id in console.busy,
    "turns": len(list_sessions(directory)),
    # Asked while nobody was watching, or before a reload: the event is
    # gone, the turn is still waiting. See approver.py.
    "approvals": console.broker.asked(str(directory)),
}
```

前端：重新同步时把它们画成卡片（§7.11）。

三个测试：等待表按会话分开；一次真的"刷新"拿得到它、回答之后它就消失；`run_turn` 真的把会话目录交给了审批者。第三个是在写变异条目时想到的：给"把 `run_turn` 里的 `thread=...` 删掉"写变异时，发现前两个测试都不经过 `run_turn`——它们各自自己造 `WebApprover`——所以那条变异不会有任何测试注意到，而那一行恰好是两半接上的地方。于是先补了测试，再跑变异检查。

```python
async def test_a_pending_approval_is_listed_for_its_own_thread_only() -> None:
    broker = ApprovalBroker()
    request = ApprovalRequest("rm -rf build", "deletes files", Risk.WRITE, None)
    asking = asyncio.ensure_future(WebApprover(broker, lambda _e: None, thread="a").ask(request))
    await asyncio.sleep(0)

    (pending,) = broker.asked("a")
    assert pending["what"] == "rm -rf build"
    assert broker.asked("b") == []

    assert broker.resolve(pending["id"], ApprovalReply(True, "rm -rf build"))
    assert (await asking).approved
    assert broker.asked("a") == []


def test_a_reload_still_shows_the_approval_the_turn_is_waiting_on(
    console: TestClient, workspace: Path
) -> None:
    """The request reached the browser as one event; a tab that was not
    watching, or reloaded, never saw it, and `GET /api/threads/{id}` -- what a
    tab rebuilds itself from -- did not mention it. The card vanished and the
    turn waited ten minutes for an answer nobody could give."""
    thread_id = _thread(console, workspace)
    state = console.app.state.console
    thread = str(state.store.thread_dir(thread_id))
    request = ApprovalRequest("make deploy", "runs the deploy", Risk.WRITE, ("make",))
    asking = console.portal.start_task_soon(
        WebApprover(state.broker, lambda _e: None, thread=thread).ask, request
    )
    for _ in range(100):
        approvals = console.get(f"/api/threads/{thread_id}").json()["approvals"]
        if approvals:
            break
    assert [a["what"] for a in approvals] == ["make deploy"]

    answer = console.post(f"/api/approvals/{approvals[0]['id']}/respond", json={"approved": False})
    assert answer.status_code == 200
    assert asking.result(timeout=5).approved is False
    assert console.get(f"/api/threads/{thread_id}").json()["approvals"] == []


async def test_a_turn_tells_its_approver_which_thread_is_asking(
    tmp_path: Path, stub_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route lists approvals by thread directory; `run_turn` is the one
    place that knows which directory a turn belongs to. The two meet only if
    the approver is built with it."""
    from minicodex.web import runtime

    built: list[str | None] = []

    class Spy(WebApprover):
        def __init__(self, *args: Any, thread: str | None = None, **kwargs: Any) -> None:
            built.append(thread)
            super().__init__(*args, thread=thread, **kwargs)

    monkeypatch.setattr(runtime, "WebApprover", Spy)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    await runtime.run_turn(
        thread_dir=tmp_path / "thread",
        workspace_root=workspace,
        question="what does this define?",
        settings=dict(DEFAULT_THREAD_SETTINGS),
        provider_name="ollama",
        base_url=stub_url,
        model="m",
        api_key=None,
        mcp_servers=[],
        broker=ApprovalBroker(),
        emit=lambda _event: None,
    )
    assert built == [str(tmp_path / "thread")]
```

> - 第二个测试里的 `console.portal.start_task_soon(...)`：`TestClient` 在另一个线程里跑着应用的事件循环，`portal` 是通往那个循环的口子。审批必须在**那个**循环上等，路由才能解开它。
> - 第三个用一个"间谍"子类替换 `runtime.WebApprover`，记下它被造出来时拿到的 `thread`。

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

（这次改写在 Linux 上重跑，和第一次一模一样。）


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


泵的核心就是这几行（`channel.py` 全文在 §6.1）：

```python
while True:
    payload = await queue.get()
    if payload is None:
        return
    # Awaited one socket at a time. Slower than a fan-out, and that
    # is the trade being made: a console driven by one person does
    # not have enough sockets for the difference to be measurable,
    # and out-of-order transcripts are not a thing a person can
    # debug from the browser.
    for socket in list(sockets):
        try:
            await socket.send_text(payload)
        except Exception:
            sockets.discard(socket)
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


（§6.1 的 `unsubscribe`：把集合、队列一起扔掉，往队列里放一个 `None` 当"停"的信号。）


用哨兵而不是 `task.cancel()`：已经排队的事件还要发给剩下的人，
取消会把它们连同队列一起丢掉。


### 6.1 代码：`channel.py`

说明。它把 §5、§6 讲过的三个问题又写了一遍——因为读代码的人不一定读过这本书；最后一段是这次改写加的（§6.2）：

```python
"""One ordered stream of events per thread, and the sockets watching it.

The first version of this was three lines inside a route: for every event, for
every socket, `asyncio.create_task(ws.send_text(payload))`. It is worth saying
exactly what is wrong with that, because nothing about it ever raises.

**Order is not preserved.** Each `create_task` schedules an independent
coroutine, and the loop is free to run them in any order once any of them
awaits -- which `send_text` does, on the first byte. So `history_item` can
reach the browser *after* the `turn_complete` that follows it, and the console
renders in arrival order. There is no exception, no dropped event and no log
line: the transcript is simply, occasionally, in the wrong order.

**A task nobody holds can be collected.** `asyncio.create_task` returns a task
the caller is expected to keep; the event loop holds only a weak reference, so
CPython is entitled to collect a running task mid-await. The documented fix is
to keep a strong reference until it is done, which is what `_pump_tasks` is.

**The subscriber table grows forever.** Discarding a socket from a set leaves
the empty set behind, one per thread ever opened, for the life of the process.

So: one `asyncio.Queue` per thread, one pump task draining it, and each send
awaited in turn. Ordering then comes from the queue rather than from luck, the
pump is the strong reference, and the queue and its set are dropped together
when the last socket goes.

`emit` stays synchronous on purpose. It is called from inside the agent -- from
`History`'s observer, three frames below `agent.run` -- and the thing it is
called from cannot await. `put_nowait` on an unbounded queue is the only shape
that fits there without the core learning what a WebSocket is.

**A pump belongs to its own queue and its own set of sockets**, which arrived
when chapter 19 was rewritten. The first version looked both up by thread id
every time. When the last socket left, it queued the stop sentinel and left the
queue in the table until the pump got round to reading it -- and a socket that
subscribed in between (a reload, a second tab, a pump still busy sending a
backlog) found that queue, was told a pump existed, and joined a pump that was
about to stop. From then on every event for that thread was dropped at `emit`,
with a subscriber registered and nothing logged: the "page stopped updating"
failure this module exists to prevent, rebuilt one layer down. Now the last
socket out detaches the queue and the set at once, so a newcomer always starts
a fresh pump.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)
```

一个 Protocol：这个模块对"socket"只要求一件事，能 `send_text`。测试里塞一个把收到的东西存进列表的假对象就行：

```python
class Socket(Protocol):
    """What this module needs from a `fastapi.WebSocket`.

    A Protocol rather than the real class so the ordering test can drive the
    pump with a list-appending fake and assert on the list. The test that
    catches an ordering bug must not itself depend on a network.
    """

    async def send_text(self, data: str) -> None: ...
```

`Channel`。三张表——会话 id → 队列、→ socket 集合、→ 泵任务——和订阅、退订：

```python
class Channel:
    """Fan one thread's events out to its sockets, in the order they happened."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[str | None]] = {}
        self._sockets: dict[str, set[Socket]] = {}
        self._pumps: dict[str, asyncio.Task[None]] = {}

    # -- subscription --------------------------------------------------------

    def subscribe(self, thread_id: str, socket: Socket) -> None:
        self._sockets.setdefault(thread_id, set()).add(socket)
        self._ensure_pump(thread_id)

    def unsubscribe(self, thread_id: str, socket: Socket) -> None:
        sockets = self._sockets.get(thread_id)
        if sockets is None:
            return
        sockets.discard(socket)
        if sockets:
            return
        # Last one out: detach the set and the queue *now*, so the next
        # subscriber starts its own pump instead of joining this one on its
        # way out. A sentinel rather than `task.cancel()`, so the pump finishes
        # what it was doing before it stops.
        self._sockets.pop(thread_id, None)
        queue = self._queues.pop(thread_id, None)
        if queue is not None:
            queue.put_nowait(None)

    def subscriber_count(self, thread_id: str) -> int:
        return len(self._sockets.get(thread_id, ()))
```

> - **`subscribe`**：`setdefault(thread_id, set())` 是"有就拿出来，没有就先放一个空集合再拿出来"，然后把这个 socket 加进去；再确保这个会话有一个泵。
> - **`unsubscribe`**：拿掉这个 socket。还有别人就到此为止；是最后一个，就把集合和队列都从表里摘掉，再往（被摘掉的）队列里放一个 `None`，让泵发完手头的东西后停下来。为什么是"摘掉"而不是"留着等泵来收"，§6.2。

发事件：

```python
def emit(self, thread_id: str, event: dict[str, Any]) -> None:
    """Queue one event. Safe to call from anywhere inside the run."""
    queue = self._queues.get(thread_id)
    if queue is None:
        # Nobody is watching this thread. The turn still runs, and the
        # browser reconstructs what it missed from the rollout file when it
        # reconnects (`GET /api/threads/{id}`), which is the same source
        # the events were derived from. Dropping here rather than buffering
        # forever is what stops a closed tab from being a memory leak.
        return
    queue.put_nowait(json.dumps(event, ensure_ascii=False, default=str))

def emitter(self, thread_id: str) -> Any:
    """A one-argument `emit` bound to a thread, for handing down into a run."""

    def send(event: dict[str, Any]) -> None:
        self.emit(thread_id, event)

    return send
```

`emit` 是**同步**函数，它只做一件事：把一段文本塞进队列。
`put_nowait` 不会阻塞也不会 `await`——所以它可以从任何地方调用，
包括 `History` 的观察者回调那种"不能 await"的地方。

> - `json.dumps(event, ensure_ascii=False, default=str)`：变成文本再进队列。`ensure_ascii=False` 让中文原样保留；`default=str` 让 JSON 不认识的东西（比如 `Path`）变成字符串，而不是报错。
> - 没人在看就直接丢：浏览器回来时会重新同步（§7）。
> - **`emitter`**：一个闭包，把 `thread_id` "绑"在里面，返回一个只收一个参数的 `send`。`run_turn` 拿到的 `emit` 就是它——Agent 那边不需要知道会话 id。

泵：

```python
def _ensure_pump(self, thread_id: str) -> None:
    if thread_id in self._queues:
        return
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    self._queues[thread_id] = queue
    sockets = self._sockets.setdefault(thread_id, set())
    task = asyncio.get_running_loop().create_task(self._pump(thread_id, queue, sockets))
    self._pumps[thread_id] = task  # the strong reference; see the docstring
    task.add_done_callback(lambda done, tid=thread_id: self._forget_pump(tid, done))

def _forget_pump(self, thread_id: str, task: asyncio.Task[None]) -> None:
    # Only if it is still the current one: a newer pump may already have
    # taken this thread's slot.
    if self._pumps.get(thread_id) is task:
        del self._pumps[thread_id]

async def _pump(
    self, thread_id: str, queue: asyncio.Queue[str | None], sockets: set[Socket]
) -> None:
    try:
        while True:
            payload = await queue.get()
            if payload is None:
                return
            # Awaited one socket at a time. Slower than a fan-out, and that
            # is the trade being made: a console driven by one person does
            # not have enough sockets for the difference to be measurable,
            # and out-of-order transcripts are not a thing a person can
            # debug from the browser.
            for socket in list(sockets):
                try:
                    await socket.send_text(payload)
                except Exception:
                    sockets.discard(socket)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A pump that dies takes the thread's whole event stream with it,
        # silently, while the turn keeps running. Logged rather than
        # swallowed, because "the browser stopped updating" is otherwise
        # indistinguishable from "the model is thinking".
        log.exception("event pump for thread %s stopped", thread_id)
    finally:
        if self._queues.get(thread_id) is queue:
            del self._queues[thread_id]
```

泵是一个**一直在跑的协程**：从队列里取一条，发给所有 socket，再取下一条。
关键在于它是**一个**协程——所以"取第 2 条"这件事必然发生在"第 1 条发完"
之后。顺序是这个结构本身保证的，不是靠调度器帮忙。

`list(...)` 那一层拷贝也是必须的：循环体里可能会 `discard` 一个 socket，
在迭代过程中改动一个集合，Python 会抛 `RuntimeError: Set changed size
during iteration`。

> - **`_ensure_pump`**：这个会话还没有队列，就建一个队列、拿到它的 socket 集合、起一个泵任务，存进 `_pumps`（强引用）。任务结束时由 `_forget_pump` 把它从表里拿掉——只在它**还是**当前那个时才拿（§6.2）。
> - **`_pump(thread_id, queue, sockets)`**：泵拿到的是**它自己的**队列和集合，不再每次按会话 id 去表里查（§6.2）。
> - 发给某个 socket 失败了（浏览器关了），就把它从集合里删掉，继续发给别人。
> - 外层两个 `except`：取消就往上抛；别的异常记一条日志——一个悄悄死掉的泵，看起来和"模型在思考"一模一样。
> - `finally`：把自己的队列从表里拿掉——同样只在表里那个还是它的时候。

关闭：

```python
async def aclose(self) -> None:
    """Stop every pump. Called from the app's shutdown hook."""
    for queue in list(self._queues.values()):
        queue.put_nowait(None)
    for task in list(self._pumps.values()):
        # `asyncio.TimeoutError` and not the builtin: on 3.10, which
        # `requires-python` promises, they are unrelated classes and the
        # builtin never fires. Chapter 15 paid for that one twice already
        # (F15-02) -- writing it correctly the first time here is free.
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 2.0)
```

WebSocket 路由，在 `routes.py` 里：

```python
@router.websocket("/ws/threads/{thread_id}")
async def thread_socket(ws: WebSocket, thread_id: str) -> None:
    console = ws.app.state.console
    await ws.accept()
    console.channel.subscribe(thread_id, ws)
    try:
        while True:
            # The console never sends anything over this socket. The loop is
            # here to notice a close: `receive_text` raises on disconnect.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        console.channel.unsubscribe(thread_id, ws)


```

> - `await ws.accept()`：同意建立连接。之后立刻订阅——这两行之间没有 `await` 让别的东西插进来。
> - 控制台从来不往这个 socket 上**收**东西。那个 `while True: await ws.receive_text()` 只是为了发现"对方断开了"：断开时它抛 `WebSocketDisconnect`。
> - `finally`：不管怎么断的，都退订。

测试用的两个假 socket：

```python
class Recorder:
    """A socket that just remembers what it was sent, in order."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, data: str) -> None:
        # A real socket awaits on the first byte, and that await is exactly
        # what let the old fan-out reorder. Yielding here means a test that
        # passes cannot be passing by accident of never suspending.
        await asyncio.sleep(0)
        self.sent.append(json.loads(data))


class Uneven:
    """A socket that takes *longer* on the events that were emitted earlier.

    `Recorder` suspends for the same length of time on every send, and that
    turns out to be why the fan-out mutation survived: a hundred tasks that
    each yield once resume in the order they were created, so
    `create_task`-per-event still delivers in order and the assertion holds.
    The test was measuring the scheduler's FIFO habit, not the pump.

    Real sockets are not uniform -- the first write on a connection pays for a
    handshake the tenth does not -- and the delay here is keyed off the event's
    own number rather than off call order, so it does not depend on which task
    happens to start first. Under the awaited pump the sends are sequential and
    arrive in order regardless; under a fan-out they arrive backwards.
    """

    def __init__(self, count: int) -> None:
        self.sent: list[dict[str, Any]] = []
        self._count = count

    async def send_text(self, data: str) -> None:
        event = json.loads(data)
        await asyncio.sleep(0.002 * (self._count - 1 - event["n"]))
        self.sent.append(event)
```

> `Uneven` 的说明讲了一个变异检查教的事：`Recorder` 每次都只让出一次，于是"每个事件一个任务"的坏写法也恰好按顺序到达——测试量的是调度器的习惯，不是泵。`Uneven` 让**先**发的事件**更慢**，坏写法就会倒过来。

顺序的四个测试，和订阅表的两个：

```python
async def test_F19_03_events_arrive_in_the_order_they_were_emitted() -> None:
    """The fault this whole module exists for.

    `for socket in sockets: create_task(socket.send_text(payload))` schedules
    one independent coroutine per event per socket. Each suspends on its first
    await, and the loop resumes them in whatever order it likes -- so a
    `history_item` can arrive after the `turn_complete` that came after it.
    Nothing raises. The transcript is simply wrong, sometimes.

    A hundred events rather than three: with three, an out-of-order run is
    likely enough to pass by luck often enough to look green.
    """
    channel = Channel()
    socket = Recorder()
    channel.subscribe("t1", socket)

    for i in range(100):
        channel.emit("t1", {"type": "history_item", "n": i})
    await asyncio.sleep(0.05)

    assert [e["n"] for e in socket.sent] == list(range(100))


async def test_F19_03_order_holds_when_the_early_events_are_the_slow_ones() -> None:
    """The same fault, with the luck taken out.

    The test above passes with the fan-out put back, because every send costs
    the same and the loop resumes same-cost tasks in creation order. This one
    makes the first event the slowest, which is the case a fan-out gets wrong
    and an awaited pump cannot: it does not schedule the second send until the
    first has returned.
    """
    count = 8
    channel = Channel()
    socket = Uneven(count)
    channel.subscribe("t1", socket)

    for i in range(count):
        channel.emit("t1", {"type": "history_item", "n": i})
    await asyncio.sleep(0.2)

    assert [e["n"] for e in socket.sent] == list(range(count))


async def test_F19_03_every_subscriber_sees_the_same_order() -> None:
    channel = Channel()
    one, two = Recorder(), Recorder()
    channel.subscribe("t1", one)
    channel.subscribe("t1", two)

    for i in range(50):
        channel.emit("t1", {"n": i})
    await asyncio.sleep(0.05)

    assert [e["n"] for e in one.sent] == [e["n"] for e in two.sent] == list(range(50))


async def test_F19_03_a_socket_that_raises_is_dropped_without_taking_the_pump_down() -> None:
    """One dead browser tab must not stop the other tab's transcript."""

    class Broken:
        async def send_text(self, data: str) -> None:
            raise ConnectionResetError("gone")

    channel = Channel()
    good = Recorder()
    channel.subscribe("t1", Broken())
    channel.subscribe("t1", good)

    for i in range(5):
        channel.emit("t1", {"n": i})
    await asyncio.sleep(0.05)

    assert [e["n"] for e in good.sent] == [0, 1, 2, 3, 4]


async def test_F19_04_the_last_unsubscribe_drops_the_thread_entirely() -> None:
    """`sockets.discard(ws)` leaves the empty set behind, one per thread ever
    opened, for the life of the process. Invisible on a laptop; a slow leak on
    anything left running."""
    channel = Channel()
    socket = Recorder()
    channel.subscribe("t1", socket)
    assert channel.subscriber_count("t1") == 1

    channel.unsubscribe("t1", socket)
    await asyncio.sleep(0.05)

    assert channel.subscriber_count("t1") == 0
    assert channel._sockets == {}
    assert channel._queues == {}
    assert channel._pumps == {}


async def test_F19_04_emitting_to_a_thread_nobody_watches_is_not_an_error() -> None:
    """The turn keeps running when the tab closes; the browser rebuilds what it
    missed from the rollout file on reconnect."""
    channel = Channel()
    channel.emit("nobody", {"type": "notice", "text": "still working"})
    await asyncio.sleep(0)
```

> - 每个测试都是 `async def`：`Channel` 要一个正在运行的事件循环。
> - `await asyncio.sleep(0)`：让出一次，让泵有机会跑。测试里常写一个循环让出好几次，等泵把队列发完。
> - 订阅表那个测试直接看三张内部表都空了——这是"不泄漏"唯一能看到的证据。

### 6.2 改写时发现：前脚刚走，后脚就来

第一版的 `unsubscribe`、泵发送的那几行、泵的 `finally`：

```python
        # Last one out: drop the set, the queue and the pump together. A
        # sentinel rather than `task.cancel()`, so anything already queued is
        # still delivered to whoever is left before the pump stops.
        self._sockets.pop(thread_id, None)
        queue = self._queues.get(thread_id)
        if queue is not None:
            queue.put_nowait(None)
```

```python
                for socket in list(self._sockets.get(thread_id, ())):
                    try:
                        await socket.send_text(payload)
                    except Exception:
                        self._sockets.get(thread_id, set()).discard(socket)
```

```python
        finally:
            self._queues.pop(thread_id, None)
```

最后一个 socket 走的时候，它往队列里放了"停"的信号，但**队列还留在表里**，等泵自己读到 `None`、在 `finally` 里删掉。

在那之前，如果又来了一个 socket——刷新页面、开第二个标签页，或者泵还在给一个慢 socket 发积压的事件——`subscribe` 调 `_ensure_pump`，看到"这个会话已经有队列了"，就直接返回。新来的 socket 加入的是一个**马上就要停**的泵。泵读到 `None`，停下，删掉队列。从此以后，这个会话的每一个事件都在 `emit` 那里被当作"没人在看"丢掉——而订阅表里明明有一个订阅者，日志里什么都没有。

写一个最小的复现：订阅一个、让出一次、退订、立刻订阅另一个，然后发一个事件：

```
$ uv run python chan19.py          # 第一版
subscribers: 1 | events b received: 0 | queue exists: False

$ uv run python chan19.py          # 修好之后
subscribers: 1 | events b received: 1 | queue exists: True
```

（`chan19.py` 就是下面那个测试的脚本版。）

这恰好是这个模块的说明第一段就在防的那种故障——"浏览器不再更新，和模型在思考分不出来"——在下面一层被重新造了出来。

修法：**每个泵绑定它自己的队列和 socket 集合**。最后一个 socket 走时，立刻把两者都从表里摘掉；之后来的 socket 一定会建一个新泵。旧泵把手头的东西发给它自己那个（已经空了的）集合，然后停下；它的 `finally` 和完成回调只在"表里那个还是我"的时候才删表项，不会误删新泵的。

```python
async def test_a_socket_that_arrives_while_the_last_one_is_leaving_still_hears_events() -> None:
    """The last socket out queued the pump's stop sentinel and left the queue
    in place; a socket that subscribed before the pump read it joined a pump
    that was about to stop, and every later event was dropped at `emit`."""
    channel = Channel()
    leaving, arriving = Recorder(), Recorder()
    channel.subscribe("t", leaving)
    await asyncio.sleep(0)
    channel.unsubscribe("t", leaving)
    channel.subscribe("t", arriving)  # before the old pump has run again
    for _ in range(5):
        await asyncio.sleep(0)
    channel.emit("t", {"type": "notice", "text": "still here"})
    for _ in range(5):
        await asyncio.sleep(0)
    assert arriving.sent == [{"type": "notice", "text": "still here"}]
    await channel.aclose()
```

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


第一版的 `onopen`（后来改过，§7.11）：

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


### 7.2 代码：前端的骨架

前端是一个独立的小项目，放在 `frontend/` 下面。先是它的依赖清单 `package.json`：

```json
{
  "name": "minicodex-console",
  "private": true,
  "version": "0.0.1",
  "type": "module",
  "description": "Chapter 19's browser front end for minicodex. Built with Vite; served by `minicodex serve` from dist/.",
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "preview": "vite preview",
    "test": "vitest run",
    "typecheck": "tsc --noEmit"
  },
  "dependencies": {
    "react": "^19.2.0",
    "react-dom": "^19.2.0"
  },
  "devDependencies": {
    "@types/react": "^19.2.0",
    "@types/react-dom": "^19.2.0",
    "@vitejs/plugin-react": "^5.0.0",
    "typescript": "^5.9.0",
    "vite": "^7.1.0",
    "vitest": "^3.2.0"
  }
}
```

> - `scripts`：`npm run dev` 起开发服务器；`npm run build` 先做类型检查（`tsc --noEmit`，只检查不输出），再用 Vite 打包；`npm run typecheck` 只做类型检查（CI 用它）。
> - `dependencies`：运行时要的——只有 React 两个包。`devDependencies`：只在开发和构建时要的。
> - `^19.2.0`：19.x 里不低于 19.2.0 的版本。确切装哪个版本，记在 `package-lock.json` 里（不贴，它是几千行自动生成的文件）。

TypeScript 的配置 `tsconfig.json`：

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUnusedLocals": true,
    "noUnusedParameters": true,
    "noFallthroughCasesInSwitch": true,
    "noEmit": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "resolveJsonModule": true,
    "allowImportingTsExtensions": true,
    "verbatimModuleSyntax": true,
    "types": ["vite/client"]
  },
  "include": ["src", "vite.config.ts"]
}
```

> 最重要的一行是 `"strict": true`：打开所有严格检查（比如"可能是 `null` 的东西不能直接用"）。`noUnusedLocals`、`noUnusedParameters`：没用到的变量和参数算错误。`"jsx": "react-jsx"`：`.tsx` 里的标签交给 React 处理。其余是"按现代浏览器和 Vite 的方式处理模块"，照抄即可。

Vite 的配置 `vite.config.ts`：

```typescript
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Two ways to run this, and the proxy is what makes the first one work.
//
//   npm run dev     -> Vite on 5173 with hot reload, /api and /ws proxied to
//                      the Python server on 8000. Two processes, instant reload.
//   npm run build   -> dist/, which `minicodex serve` mounts at / itself.
//                      One process, one port, nothing to install to run it.
//
// `ws: true` on the /ws entry is not optional: without it the proxy answers the
// upgrade request with a normal HTTP response and the socket never opens, which
// looks exactly like a server that accepted the connection and then went quiet.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
  build: {
    outDir: "dist",
    // Chapter 15's argument about artefacts, one layer up: a stack trace from a
    // minified bundle with no map is a bug report nobody can act on, and the
    // maps are not served to anyone who does not ask for them.
    sourcemap: true,
  },
});
```

> - 开发时（`npm run dev`）前端在 5173 端口，`/api` 和 `/ws` 转发给 8000 端口的 Python 服务器——这叫**代理**。注释讲了 `ws: true` 为什么不能少。
> - `sourcemap: true`：打包时额外生成一份"压缩后的代码 ↔ 源代码"的对照表，浏览器里报错时能看到是源文件哪一行。

页面本身 `index.html`——只有一个空的 `<div id="root">`，剩下的全由 React 画：

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <!-- Without this the mobile layout never applies: the old console shipped a
         whole `@media (max-width: 860px)` drawer that a phone rendered at
         desktop width, because the page had no viewport meta and no doctype at
         all.  Neither omission produces a warning anywhere. -->
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>minicodex Console</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```

> 注释讲了 `viewport` 那一行：没有它，手机会按桌面宽度显示页面，为手机写的样式永远不生效——而且不会有任何警告。旧控制台就是这样。

入口 `src/main.tsx`：

```tsx
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./theme.css";
import "./console.css";

const root = document.getElementById("root");
if (!root) throw new Error("index.html has no #root");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
```

> - `document.getElementById("root")`：找到那个空 div；`createRoot(root).render(<App />)`：让 React 接管它，从 `App` 组件开始画。
> - `import "./theme.css"`：样式也是用 `import` 引入的，Vite 打包时会把它们收集起来。
> - **`<StrictMode>`**：React 的"严格模式"。开发时它会故意把每个 `useEffect` **执行、清理、再执行一次**，用来暴露"清理没写好"的 bug。这意味着开发时每打开一个会话，WebSocket 都会**连上、断开、再连上**——正是 §6.2 那种"前脚刚走、后脚就来"的连接方式。（会不会真的撞上那个竞态，取决于服务器那边几件事发生的先后；这一点没有测过。）

### 7.3 代码：`types.ts`——前后端约好的形状

前端要知道后端返回的 JSON 长什么样。这些形状写在 `src/types.ts` 里：

```typescript
// The shapes the backend actually sends. Written out rather than inferred,
// because the previous console and its backend disagreed about four field
// names and nothing said so -- `mark` events were emitted and dropped,
// `turn_complete.model` was sent and never read, and the skills endpoint had
// no caller at all. A type is the cheapest place for that disagreement to
// become visible.

export type SandboxMode = "read-only" | "workspace-write" | "full-access";
export type ApprovalPolicy = "never" | "on-request" | "unless-trusted";
export type StepStatus = "pending" | "in_progress" | "completed";

export interface ThreadSettings {
  sandbox_mode: SandboxMode;
  approval_policy: ApprovalPolicy;
  context_window: number | null;
  memory: boolean;
  remember: boolean;
  skills: boolean;
}

export interface Thread {
  id: string;
  workspace: string;
  title: string;
  created: number;
  settings: ThreadSettings;
  message_count?: number;
  model?: string | null;
  busy?: boolean;
}

export interface WorkspaceGroup {
  path: string;
  threads: Thread[];
}

export interface ToolCall {
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export type HistoryItem =
  | { type: "user"; text: string }
  | { type: "assistant"; text?: string; tool_calls?: ToolCall[] }
  | { type: "tool_result"; call_id: string; name?: string; content: string }
  | { type: "system_note"; text: string }
  | { type: "developer_note"; text: string };

export interface PlanStep {
  text: string;
  status: StepStatus;
}

export interface Provider {
  id: string;
  name: string;
  provider: string;
  base_url: string;
  model: string;
  api_key: boolean;
  active: boolean;
}

export interface McpServer {
  id: string;
  name: string;
  command: string[];
  env: Record<string, string>;
  cwd: string | null;
  startup_timeout: number;
  tool_timeout: number;
  enabled: boolean;
}

export interface Rule {
  index: number;
  words: string[];
  scope: "session" | "project";
  prompted_by: string;
  created_at: string;
  describe: string;
}

export interface MemoryEntry {
  id: string;
  title: string;
  headline: string;
  lines: number;
  usage: { uses?: number; last_used?: string } | null;
}

export interface Skill {
  name: string;
  description: string;
  body: string;
  path: string;
  catalog_line: string;
}

/** One pending approval, as the socket sends it and as `GET /api/threads/{id}` repeats it. */
export interface ApprovalRequestEvent {
  type: "approval_request";
  id: string;
  what: string;
  reason: string;
  risk: string;
  suggested_rule: string[] | null;
}

/** Every event the backend can put on the socket. Exhaustive on purpose. */
export type ServerEvent =
  | { type: "turn_started"; text: string }
  | { type: "turn_finished" }
  // `index` is the item's place in the history the thread loads back as, and
  // `position` is how long that history is once a mark is written; see
  // useThread for what they are for.
  | { type: "history_item"; index: number; item: HistoryItem }
  | { type: "mark"; mark: string; payload: Record<string, unknown>; position: number }
  | { type: "notice"; text: string }
  | { type: "error"; text: string }
  | { type: "mcp_status"; name: string; status: string; detail?: string; tools?: number }
  | { type: "plan_updated"; steps: PlanStep[]; revisions: number }
  | { type: "session_mode_changed"; from: SandboxMode; mode: SandboxMode; policy: ApprovalPolicy }
  | ApprovalRequestEvent
  | { type: "approval_resolved"; id: string; approved: boolean; remember: string | null }
  | { type: "approval_timeout"; id: string }
  | {
      type: "turn_complete";
      final_text: string;
      stop_reason: string;
      turns_used: number;
      model: string;
      plan: PlanStep[];
      reports: string[];
      transcript: string;
      session_file: string;
      citations: string[];
    };
```

> - 开头注释说了为什么值得写这些类型：前端和后端对一个形状的理解不一致，是一类在运行时才暴露、而且不报错的故障；类型检查是最便宜的发现它的地方。
> - **`HistoryItem`**：一条历史记录，五种之一，用 `type` 字段区分——和 Python 那边 `_dump_item` 写出来的一样。
> - **`ServerEvent`**：WebSocket 上可能出现的**每一种**事件。`history_item` 的 `index`、`mark` 的 `position`、`ApprovalRequestEvent` 是这次改写加的（§7.11）。
> - 写成一个"联合类型"的好处在 `useThread` 的 `switch (event.type)` 里：每个 `case` 里，TypeScript 知道 `event` 具体是哪一种，有哪些字段。

### 7.4 代码：`api.ts`——所有 HTTP 请求

```typescript
import type {
  ApprovalPolicy,
  ApprovalRequestEvent,
  McpServer,
  MemoryEntry,
  Provider,
  Rule,
  SandboxMode,
  Skill,
  Thread,
  ThreadSettings,
  WorkspaceGroup,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    // FastAPI puts the message in `detail`. Reading it rather than showing
    // "500" is the difference between "no such directory: /tpm/foo" and a
    // number the person cannot act on.
    let detail = res.statusText;
    try {
      const payload = await res.json();
      if (payload && typeof payload.detail === "string") detail = payload.detail;
    } catch {
      /* not JSON; keep the status text */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204 || res.status === 202) return undefined as T;
  return (await res.json()) as T;
}

export interface PolicyInfo {
  sandbox_modes: SandboxMode[];
  approval_policies: ApprovalPolicy[];
  defaults: ThreadSettings;
  tables: Record<string, string[]>;
}

export const api = {
  policy: () => call<PolicyInfo>("GET", "/api/policy"),
  tools: () => call<{ name: string; description: string }[]>("GET", "/api/tools"),

  workspaces: () => call<WorkspaceGroup[]>("GET", "/api/workspaces"),
  thread: (id: string) =>
    call<
      Thread & {
        items: unknown[];
        marks: unknown[];
        busy: boolean;
        turns: number;
        approvals: ApprovalRequestEvent[];
      }
    >(
      "GET",
      `/api/threads/${id}`,
    ),
  createThread: (workspace: string, title: string, settings: Partial<ThreadSettings>) =>
    call<Thread>("POST", "/api/threads", { workspace, title, settings }),
  patchThread: (id: string, patch: { title?: string; settings?: Partial<ThreadSettings> }) =>
    call<Thread>("PATCH", `/api/threads/${id}`, patch),
  deleteThread: (id: string) => call<{ ok: boolean }>("DELETE", `/api/threads/${id}`),
  sessions: (id: string) =>
    call<
      { session_id: string; created: number; model: string; messages: number }[]
    >("GET", `/api/threads/${id}/sessions`),
  fork: (id: string, session: string, upto: number | null) =>
    call<Thread>("POST", `/api/threads/${id}/fork`, { session, upto }),

  send: (id: string, text: string) =>
    call<void>("POST", `/api/threads/${id}/messages`, { text }),
  cancel: (id: string) => call<{ ok: boolean }>("POST", `/api/threads/${id}/cancel`),

  respond: (
    approvalId: string,
    approved: boolean,
    command: string,
    remember: "session" | "project" | null,
  ) => call<{ ok: boolean }>("POST", `/api/approvals/${approvalId}/respond`, {
    approved,
    command,
    remember,
  }),

  rules: (id: string) => call<Rule[]>("GET", `/api/threads/${id}/rules`),
  revokeRule: (id: string, index: number) =>
    call<{ revoked: string }>("DELETE", `/api/threads/${id}/rules/${index}`),

  memory: (id: string) =>
    call<{
      directory: string;
      summary?: string;
      describe?: string;
      error?: string;
      entries: MemoryEntry[];
    }>("GET", `/api/threads/${id}/memory`),
  memoryJobs: (id: string) =>
    call<{ counts: Record<string, number>; rows: unknown[] }>(
      "GET",
      `/api/threads/${id}/memory/jobs`,
    ),
  forgetAll: (id: string) =>
    call<{ removed: string[] }>("POST", `/api/threads/${id}/memory/forget-all`),

  skills: (id: string) =>
    call<{
      supported: boolean;
      directory: string;
      describe: string;
      skipped: string[];
      items: Skill[];
    }>("GET", `/api/threads/${id}/skills`),
  plugins: () => call<{ supported: boolean; items: unknown[] }>("GET", "/api/plugins"),

  providers: () => call<Provider[]>("GET", "/api/providers"),
  addProvider: (body: Omit<Provider, "id" | "active" | "api_key"> & { api_key: string | null }) =>
    call<Provider>("POST", "/api/providers", body),
  activateProvider: (id: string) =>
    call<{ ok: boolean }>("POST", `/api/providers/${id}/activate`),
  deleteProvider: (id: string) => call<{ ok: boolean }>("DELETE", `/api/providers/${id}`),

  mcp: () => call<McpServer[]>("GET", "/api/mcp"),
  addMcp: (body: {
    name: string;
    command: string;
    env: Record<string, string>;
    cwd: string | null;
    startup_timeout: number;
    tool_timeout: number;
  }) => call<McpServer>("POST", "/api/mcp", body),
  toggleMcp: (id: string, enabled: boolean) =>
    call<McpServer>("POST", `/api/mcp/${id}/toggle`, { enabled }),
  deleteMcp: (id: string) => call<{ ok: boolean }>("DELETE", `/api/mcp/${id}`),
};
```

> - **`ApiError`**：一个带状态码的错误类。`readonly status: number` 写在构造函数参数里，是"顺便把它存成一个只读属性"的简写。
> - **`call<T>(method, path, body?)`**：所有请求都走它。`fetch` 是浏览器自带的发 HTTP 请求的函数。有请求体就加 `Content-Type: application/json` 并把对象变成 JSON 文本。
> - 失败时（`!res.ok`，状态码不是 2xx）读 FastAPI 放在 `detail` 里的那句话，抛出去——界面上显示的是"no such directory: /tpm/foo"，而不是一个数字。
> - `202` 和 `204` 没有响应体，直接返回。
> - **`api`**：一个对象，每个属性是一个函数，对应后端的一个路由。`<T>` 是"这个请求返回什么形状"，只给类型检查用，运行时不检查。

### 7.5 代码：`useThread.ts`——一个会话的实时视图

这是前端最重要的文件：打开一个会话，就用它连上 WebSocket、把事件变成屏幕上的一条条内容、断线重连。

类型和常量：

```typescript
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type {
  ApprovalRequestEvent,
  HistoryItem,
  PlanStep,
  SandboxMode,
  ServerEvent,
} from "./types";

/** One renderable thing in the transcript, in arrival order. */
export type Entry =
  // `pending`: typed here and not yet written down by the server -- the echo
  // that `send` shows before the request has even gone out.
  | { kind: "item"; key: string; item: HistoryItem; pending?: boolean }
  | { kind: "note"; key: string; text: string; tone: "plain" | "danger" }
  | { kind: "mark"; key: string; mark: string; payload: Record<string, unknown> }
  | {
      kind: "approval";
      key: string;
      id: string;
      what: string;
      reason: string;
      risk: string;
      suggested_rule: string[] | null;
      status: null | "approved" | "denied" | "timeout" | "failed";
      note?: string;
    };

/**
 * `Omit<Entry, "key">` does not do what it looks like it does: over a union it
 * collapses to the keys every member shares, so it accepts `{kind: "note"}` and
 * rejects `text`. The conditional makes it distribute over the members instead.
 */
type Unkeyed<T> = T extends unknown ? Omit<T, "key"> : never;

export type Connection = "connecting" | "open" | "retrying" | "closed";

/** Reconnect backoff, in milliseconds, by consecutive failure count. */
const BACKOFF = [500, 1000, 2000, 4000, 8000, 15000];

let seq = 0;
const nextKey = () => `e${++seq}`;

export interface ThreadView {
  entries: Entry[];
  results: Record<string, string>;
  plan: PlanStep[];
  mode: SandboxMode | null;
  busy: boolean;
  connection: Connection;
  reports: string[];
  send: (text: string) => Promise<void>;
  stop: () => Promise<void>;
  respond: (
    id: string,
    approved: boolean,
    command: string,
    remember: "session" | "project" | null,
  ) => Promise<void>;
  error: string | null;
}

```

> - **`Entry`**：对话流里要画的一项，四种：一条历史记录、一条提示、一个标记、一张审批卡片。每项都有一个 `key`（见下面）。`pending` 是改写时加的（§7.11）。
> - **`Unkeyed<T>`**：注释讲了一个 TypeScript 的坑：在联合类型上用 `Omit<Entry, "key">`，结果只剩**所有成员共有**的字段。写成"条件类型"，它就会对每个成员分别去掉 `key`。第一次构建时 `tsc` 就抓到了这个（§17 的清单外故障）。
> - **`BACKOFF`**：断线后第 1、2、3……次重连前等多久（毫秒），越来越久，最多 15 秒。
> - **`nextKey`**：一个全局递增的计数器，产生 `e1`、`e2`……

说明——第一版写的是重连，后半段是这次改写加的：

```typescript
/**
 * Subscribe to one thread: load its history, follow its socket, reconnect.
 *
 * The reconnect is the point. The previous console set `onmessage` and nothing
 * else -- no `onclose`, no `onerror` -- so a `--reload` on the server, a laptop
 * sleeping, or any dropped connection left the page showing `thinking…`
 * forever with no way back but F5. Worse, it was silent: the transcript simply
 * stopped growing while the turn kept running on the server.
 *
 * Reconnecting is not enough on its own, because the backend drops events for a
 * thread nobody is watching (see channel.py). So every successful reconnect is
 * followed by a **resync** from `GET /api/threads/{id}`, which reads the same
 * rollout file the events were derived from. The socket is an optimisation over
 * polling that file; it is not the only copy of the truth, and that is what
 * makes losing it survivable.
 *
 * Three things here arrived when chapter 19 was rewritten, all found by
 * driving the built console in a real browser rather than by reading it:
 *
 * - **Every item the server streams carries its `index`** in the history the
 *   thread loads back as, and `knownRef` is how many of those the screen
 *   already shows. A turn begins by replaying the whole conversation into its
 *   new rollout file, and the first version drew every replayed item again:
 *   after the second message the page showed the first exchange twice.
 * - **Subscribe, then fetch, and hold events until the fetch lands.** The
 *   first version fetched first and subscribed after, the very gap the comment
 *   on `onopen` warns about, on the path every page load takes: a
 *   `turn_finished` sent in between left the composer disabled for good.
 *   Events that arrive while a snapshot is in flight are buffered and applied
 *   on top of it; the index says which of them it already contains.
 * - **The snapshot includes pending approvals.** Without them a reload made
 *   the card vanish while the turn waited on it for ten minutes.
 */
```

hook 的开头：状态、引用、`push`：

```typescript
export function useThread(threadId: string | null): ThreadView {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [results, setResults] = useState<Record<string, string>>({});
  const [plan, setPlan] = useState<PlanStep[]>([]);
  const [mode, setMode] = useState<SandboxMode | null>(null);
  const [busy, setBusy] = useState(false);
  const [connection, setConnection] = useState<Connection>("closed");
  const [reports, setReports] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  /** How many history positions the screen already reflects. */
  const knownRef = useRef(0);
  /** Events that arrived while a snapshot was being fetched. */
  const bufferRef = useRef<ServerEvent[] | null>(null);

  const push = useCallback((entry: Unkeyed<Entry>) => {
    setEntries((current) => [...current, { ...entry, key: nextKey() } as Entry]);
  }, []);

```

**`useState` vs `useRef`。** `useState` 变了会触发重新渲染；`useRef` 不会。
所以"要画在屏幕上的东西"（entries、busy、connection）用 `useState`，
"只是内部记账"（socket 对象、重试次数、定时器 id）用 `useRef`——
用错了会导致每次重试都把整个页面重画一遍。

> - 画在屏幕上的：对话流（`entries`）、工具结果（`results`，按调用 id 存）、计划、沙箱模式、忙不忙、连接状态、报告、错误。
> - 只是内部记账的：socket 对象、重试次数、定时器 id、`knownRef`（屏幕上已经有多少条历史）、`bufferRef`（同步期间先存起来的事件）。
> - **`push`**：在对话流末尾加一项。`setEntries((current) => [...current, ...])` 是"根据**当前**的值算出新值"的写法——几个更新挨在一起时，每一个都看得到前一个的结果。

**`resync`：从快照重建。**

```typescript
const resync = useCallback(async (id: string) => {
  const record = await api.thread(id);
  const items = record.items as HistoryItem[];
  knownRef.current = items.length;
  const answers: Record<string, string> = {};
  const calls = new Set<string>();
  for (const item of items) {
    if (item.type === "tool_result") answers[item.call_id] = item.content;
    if (item.type === "assistant") {
      for (const call of item.tool_calls ?? []) calls.add(call.call_id);
    }
  }
  // A tool result is drawn inside the call it answers, so it is not its own
  // entry -- unless the call it belongs to is not in this history, which is
  // possible after an interrupted turn was partly dropped on resume.
  const fresh: Entry[] = items
    .filter((item) => item.type !== "tool_result" || !calls.has(item.call_id))
    .map((item) => ({ kind: "item", key: nextKey(), item }));
  for (const asked of record.approvals ?? []) fresh.push(approvalEntry(asked));
  setEntries(fresh);
  setResults(answers);
  setBusy(record.busy);
  setMode(record.settings.sandbox_mode);
}, []);

```

> - `api.thread(id)`：拉一次 `GET /api/threads/{id}`。
> - 先把所有工具结果按调用 id 收进 `answers`，记下所有出现过的调用 id。
> - `fresh`：要画的条目。工具结果通常画在它所属的那次调用里面，不单独成一项；只有找不到所属调用的（被打断后丢掉了一半）才单独画。
> - 再把还在等的审批画成卡片（§4.3、§7.11）。
> - `knownRef.current = items.length`：屏幕上现在已经有这么多条历史了（§7.11）。

**`apply`：把一个事件变成屏幕上的变化。**

```typescript
const apply = useCallback(
  (event: ServerEvent) => {
    switch (event.type) {
      case "turn_started":
        setBusy(true);
        setReports([]);
        break;
      case "turn_finished":
        setBusy(false);
        break;
      case "history_item": {
        // Already on screen: a replay of the conversation into this turn's
        // file, or a compaction's new baseline, or part of a snapshot.
        if (event.index < knownRef.current) return;
        knownRef.current = event.index + 1;
        const item = event.item;
        if (item.type === "tool_result") {
          // Folded into the call it answers rather than appended as its own
          // block, so a turn reads as "it ran this, and got that".
          setResults((current) => ({ ...current, [item.call_id]: item.content }));
          return;
        }
        // The optimistic echo is replaced by the real thing rather than
        // duplicated: the server's copy is the one that got written down.
        // Found by text among the pending entries, not by "is it the last
        // entry": the first version checked the last one, and a turn's first
        // item is often a note, so the question was drawn twice.
        setEntries((current) => {
          const echo =
            item.type === "user"
              ? current.findIndex(
                  (entry) =>
                    entry.kind === "item" &&
                    entry.pending === true &&
                    entry.item.type === "user" &&
                    entry.item.text === item.text,
                )
              : -1;
          const rest = echo >= 0 ? current.filter((_, i) => i !== echo) : current;
          return [...rest, { kind: "item", key: nextKey(), item }];
        });
        break;
      }
      case "mark":
        // After a compaction the history is the new baseline, and the next
        // new item is numbered from there.
        if (event.mark === "compacted") knownRef.current = event.position;
        push({ kind: "mark", mark: event.mark, payload: event.payload });
        break;
      case "notice":
        push({ kind: "note", text: event.text, tone: "plain" });
        break;
      case "error":
        push({ kind: "note", text: event.text, tone: "danger" });
        setBusy(false);
        break;
      case "mcp_status":
        push({
          kind: "note",
          tone: event.status === "connected" ? "plain" : "danger",
          text:
            event.status === "connected"
              ? `mcp: ${event.name} connected, ${event.tools} tool(s)`
              : `mcp: ${event.name} unavailable -- ${event.detail}`,
        });
        break;
      case "plan_updated":
        setPlan(event.steps);
        break;
      case "session_mode_changed":
        setMode(event.mode);
        push({
          kind: "note",
          tone: "danger",
          text: `permissions changed: ${event.from} -> ${event.mode}`,
        });
        break;
      case "approval_request":
        // Possibly already drawn from a snapshot taken while it was pending.
        setEntries((current) =>
          current.some((entry) => entry.kind === "approval" && entry.id === event.id)
            ? current
            : [...current, approvalEntry(event)],
        );
        break;
      case "approval_resolved":
      case "approval_timeout":
        setEntries((current) =>
          current.map((entry) =>
            entry.kind === "approval" && entry.id === event.id && entry.status === null
              ? {
                  ...entry,
                  status:
                    event.type === "approval_timeout"
                      ? "timeout"
                      : event.approved
                        ? "approved"
                        : "denied",
                }
              : entry,
          ),
        );
        break;
      case "turn_complete":
        setPlan(event.plan);
        setReports(event.reports);
        setBusy(false);
        break;
    }
  },
  [push],
);
```

> - 一个大 `switch`，每种事件一个 `case`。
> - `turn_started` / `turn_finished` / `turn_complete`：开关"忙"状态；`turn_complete` 顺便带来计划和报告。
> - **`history_item`**：先看 `index`——屏幕上已经有了就跳过（§7.11）。工具结果放进 `results`，不单独成一项。用户消息会**替换**掉那个"刚发出去、还没被服务器写下"的乐观回显（`pending`），而不是再画一条（§7.11）。
> - `mark`：画一个标记；如果是 `compacted`，把 `knownRef` 设成压缩后的长度（§7.11）。
> - `notice` / `error` / `mcp_status`：画一条提示，错误用红色。
> - `plan_updated` / `session_mode_changed`：更新计划面板、沙箱模式（后者还画一条醒目的提示——权限在这一轮里变了）。
> - `approval_request`：画一张审批卡片（已经从快照里画过就不再画）。`approval_resolved` / `approval_timeout`：找到那张卡片，改它的状态。

**`useEffect`：连接、重连、清理。**

```typescript
useEffect(() => {
  if (!threadId) {
    setEntries([]);
    setResults({});
    setPlan([]);
    setReports([]);
    setBusy(false);
    setConnection("closed");
    return;
  }

  let cancelled = false;

  const open = () => {
    if (cancelled) return;
    setConnection(retriesRef.current === 0 ? "connecting" : "retrying");
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(`${scheme}//${location.host}/ws/threads/${threadId}`);
    socketRef.current = socket;

    socket.onopen = () => {
      if (cancelled) return;
      setConnection("open");
      retriesRef.current = 0;
      // Resync *after* the socket is listening, not before: doing it the
      // other way round leaves a gap where an event fires between the fetch
      // and the subscription and is lost with nothing to notice it. Every
      // open, the first one included, and whatever arrives meanwhile waits.
      bufferRef.current = [];
      void resync(threadId)
        .catch((exc: Error) => setError(exc.message))
        .finally(() => {
          const waiting = bufferRef.current ?? [];
          bufferRef.current = null;
          if (!cancelled) waiting.forEach(apply);
        });
    };
    socket.onmessage = (evt) => {
      if (cancelled) return;
      let event: ServerEvent;
      try {
        event = JSON.parse(evt.data) as ServerEvent;
      } catch {
        return; // a frame we cannot parse is not worth taking the page down for
      }
      if (bufferRef.current !== null) bufferRef.current.push(event);
      else apply(event);
    };
    socket.onclose = () => {
      if (cancelled) return;
      const wait = BACKOFF[Math.min(retriesRef.current, BACKOFF.length - 1)];
      retriesRef.current += 1;
      setConnection("retrying");
      timerRef.current = window.setTimeout(open, wait);
    };
    socket.onerror = () => socket.close();
  };

  setEntries([]);
  setResults({});
  setPlan([]);
  setReports([]);
  setError(null);
  retriesRef.current = 0;
  knownRef.current = 0;
  bufferRef.current = null;
  open();

  return () => {
    cancelled = true;
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket) {
      socket.onclose = null; // do not schedule a retry for a deliberate close
      socket.close();
    }
  };
}, [threadId, apply, resync]);
```

> - 没有打开任何会话（`threadId` 是 `null`）：把一切清空，结束。
> - `open()`：建一个 WebSocket，地址是 `ws://本机/ws/threads/<id>`（页面是 `https` 时用 `wss`）。
> - **`onopen`**：连上了。**每一次**连上（包括第一次）都开始缓冲事件、拉一次快照；快照到了，再把缓冲的事件按顺序应用上去。为什么是这个顺序，§7.11。
> - `onmessage`：收到一条，解析 JSON；正在同步就先存起来，否则直接应用。
> - `onclose`：断了。按 `BACKOFF` 等一会儿再 `open()`。`onerror`：出错就关掉，交给 `onclose` 处理。
> - effect 的主体最后调一次 `open()`。
> - `return () => {...}`：**清理函数**——切换到别的会话、或者组件被移除时执行。

`socket.onclose = null` 这一行是**必须**的，而且它和 §7 里嘲笑过的那一行
长得一模一样——区别是这里 `onclose` 真的被赋过值。不清掉它，
主动切换 thread 时的那次 close 会被重连逻辑当成掉线，然后往一个已经不看的
thread 上重连。


发消息、停止、回答审批：

```typescript
  const send = useCallback(
    async (text: string) => {
      if (!threadId) return;
      setError(null);
      // Shown before the request, so a dead socket or a 409 cannot make the
      // typed message disappear with nothing on screen to say it existed.
      push({ kind: "item", item: { type: "user", text }, pending: true });
      setBusy(true);
      try {
        await api.send(threadId, text);
      } catch (exc) {
        setBusy(false);
        setError((exc as Error).message);
      }
    },
    [threadId, push],
  );

  const stop = useCallback(async () => {
    if (!threadId) return;
    try {
      await api.cancel(threadId);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, [threadId]);

  const respond = useCallback(
    async (
      id: string,
      approved: boolean,
      command: string,
      remember: "session" | "project" | null,
    ) => {
      try {
        await api.respond(id, approved, command, remember);
      } catch (exc) {
        // The card is *not* marked resolved before this returns. Doing that
        // was optimistic in the wrong direction: the turn stays suspended on a
        // reply the server never received, and the card said "Approved."
        setEntries((current) =>
          current.map((entry) =>
            entry.kind === "approval" && entry.id === id
              ? { ...entry, status: "failed", note: (exc as Error).message }
              : entry,
          ),
        );
      }
    },
    [],
  );

  return {
    entries,
    results,
    plan,
    mode,
    busy,
    connection,
    reports,
    send,
    stop,
    respond,
    error,
  };
}
```

> - **`send`**：先在屏幕上画出这条消息（标记为 `pending`），再发请求。注释讲了为什么先画：请求失败了，用户至少还看得到自己打了什么。
> - **`respond`**：回答审批。失败时把卡片标成"发送失败"——而**不是**先乐观地标成"已批准"。注释讲了反过来做的后果：卡片说"已批准"，服务器却从没收到。

最后一个小工具：把一个审批请求事件变成一张卡片：

```typescript
function approvalEntry(event: ApprovalRequestEvent): Entry {
  return {
    kind: "approval",
    key: nextKey(),
    id: event.id,
    what: event.what,
    reason: event.reason,
    risk: event.risk,
    suggested_rule: event.suggested_rule,
    status: null,
  };
}
```

**为什么 `entries` 里每项都有一个 `key`。** React 用 key 判断"这一项还是
上次那一项吗"。用数组下标当 key，在列表**中间插入**时会让 React 认为
后面每一项都变了——表现是输入框失焦、展开状态丢失。所以这里用一个
全局自增的 `e1`、`e2`：

```typescript
let seq = 0;
const nextKey = () => `e${++seq}`;
```

### 7.6 代码：`Chat.tsx`——把对话流画出来

两个小组件：一个把工具参数缩成一行，一个画一次工具调用：

```tsx
import { useEffect, useRef, useState } from "react";
import type { Entry } from "../useThread";
import type { HistoryItem, PlanStep, ToolCall } from "../types";

function summarise(args: Record<string, unknown>): string {
  try {
    const text = JSON.stringify(args);
    return text.length > 90 ? `${text.slice(0, 87)}...` : text;
  } catch {
    return "";
  }
}

function ToolCallBlock({ call, result }: { call: ToolCall; result?: string }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="toolcall">
      <div className="toolcall-head" onClick={() => setOpen((v) => !v)}>
        <span className="tc-tag">{call.name}</span>
        <span className="tc-args">{summarise(call.arguments)}</span>
        {result === undefined && <span className="tc-spin" aria-label="running" />}
      </div>
      {open && <pre>{result ?? "running…"}</pre>}
    </div>
  );
}

```

> - `ToolCallBlock({ call, result })`：组件的参数是一个对象，这里直接"解构"出两个字段。`open` 控制展开还是折叠，点标题行切换。
> - `result === undefined`：结果还没来，画一个转圈的小图标，下面显示 `running…`。

一条历史记录：

```tsx
function Item({ item, results }: { item: HistoryItem; results: Record<string, string> }) {
  if (item.type === "user") {
    return (
      <div className="msg msg-user">
        <span className="msg-role">You</span>
        <div className="msg-bubble">{item.text}</div>
      </div>
    );
  }
  if (item.type === "assistant") {
    return (
      <div className="msg msg-agent">
        <span className="msg-role">minicodex</span>
        <div className="msg-body">
          {item.text && <div>{item.text}</div>}
          {(item.tool_calls ?? []).map((call) => (
            <ToolCallBlock key={call.call_id} call={call} result={results[call.call_id]} />
          ))}
        </div>
      </div>
    );
  }
  if (item.type === "system_note" || item.type === "developer_note") {
    return <div className="msg-note">{item.text}</div>;
  }
  // A bare `tool_result` with no call to fold into: possible on a resync, if
  // the rollout's matching assistant item was in the part that got dropped.
  return (
    <div className="toolcall">
      <div className="toolcall-head">
        <span className="tc-tag">{item.name ?? "tool result"}</span>
      </div>
      <pre>{item.content}</pre>
    </div>
  );
}
```

> 按 `item.type` 分五种画法。助手消息里如果有工具调用，每个调用画一个 `ToolCallBlock`，结果从 `results` 里按调用 id 取。

标记（§9）和审批卡片：

```tsx
/**
 * The three marks the agent writes, rendered.
 *
 * These were emitted by the backend and dropped by the frontend entirely, so a
 * run could compact half its history away, or hit its turn budget, with nothing
 * on screen to say so. They are the only signal that the conversation on screen
 * is no longer the conversation the model is being sent.
 */
function Mark({ mark, payload }: { mark: string; payload: Record<string, unknown> }) {
  const text =
    mark === "compacted"
      ? `history compacted — ${payload.replaced ?? "?"} message(s) replaced by a summary (generation ${payload.generation ?? "?"})`
      : mark === "budget_exhausted"
        ? `turn budget exhausted at turn ${payload.turn ?? "?"} — the model stopped because it ran out of turns, not because it finished`
        : mark === "interrupted"
          ? `interrupted at turn ${payload.turn ?? "?"} — the next message in this thread resumes from here`
          : `${mark}: ${JSON.stringify(payload)}`;
  return <div className="msg-mark">{text}</div>;
}

function ApprovalCard({
  entry,
  onRespond,
}: {
  entry: Extract<Entry, { kind: "approval" }>;
  onRespond: (
    id: string,
    approved: boolean,
    command: string,
    remember: "session" | "project" | null,
  ) => Promise<void>;
}) {
  const [command, setCommand] = useState(entry.what);
  const [sending, setSending] = useState(false);
  const risky = /high|critical|interpreter|network/i.test(entry.risk);
  const resolved = entry.status !== null;

  const answer = async (approved: boolean, remember: "session" | "project" | null) => {
    setSending(true);
    await onRespond(entry.id, approved, command, remember);
    setSending(false);
  };

  return (
    <div className={`approval-card${risky ? " risk-high" : ""}${resolved ? " resolved" : ""}`}>
      <div className="ac-title">minicodex wants to run something</div>
      <div className="ac-reason">
        {entry.reason} (risk: {entry.risk.toLowerCase()})
      </div>
      {/* An editable command, because approving something slightly different
          from what was asked is a real answer -- `ApprovalReply.command` is
          what actually runs. */}
      <input
        className="ac-cmd"
        value={command}
        disabled={resolved || sending}
        onChange={(e) => setCommand(e.target.value)}
      />
      <div className="ac-actions">
        <button
          className="btn btn-primary btn-sm"
          disabled={sending}
          onClick={() => void answer(true, null)}
        >
          Approve once
        </button>
        {entry.suggested_rule && entry.suggested_rule.length > 0 && (
          <>
            <button
              className="btn btn-sm"
              disabled={sending}
              onClick={() => void answer(true, "session")}
            >
              Always this session ({entry.suggested_rule.join(" ")})
            </button>
            {/* The backend has always accepted "project"; the old card never
                offered it, so the durable half of chapter 5's rule store was
                unreachable from the browser. */}
            <button
              className="btn btn-sm"
              disabled={sending}
              onClick={() => void answer(true, "project")}
              title="Remembered on disk for this workspace. Revoke under Extensions → Rules."
            >
              Always in this project
            </button>
          </>
        )}
        <button
          className="btn btn-danger btn-sm"
          disabled={sending}
          onClick={() => void answer(false, null)}
        >
          Deny
        </button>
      </div>
      <div className="ac-status">
        {entry.status === "approved" && "Approved."}
        {entry.status === "denied" && "Denied."}
        {entry.status === "timeout" && "Timed out -- treated as denied."}
        {entry.status === "failed" && `Failed to send: ${entry.note}`}
      </div>
    </div>
  );
}
```

> - `Mark`：三种标记各一句人话；不认识的就原样显示。
> - `ApprovalCard`：命令是一个**可编辑**的输入框（§15 的 R4）。`answer(approved, remember)` 调 `onRespond`，期间按钮禁用。"总是允许"两个按钮只在后端给了建议规则时才出现，因为第 5 章有些命令不允许存成规则。

计划面板和整个对话区：

```tsx
export function PlanPanel({ steps }: { steps: PlanStep[] }) {
  if (steps.length === 0) return null;
  return (
    <div className="plan-panel">
      <div className="pop-label">Plan</div>
      <ul>
        {steps.map((step, i) => (
          <li key={i} className={`plan-step plan-${step.status}`}>
            <span className="plan-box" aria-hidden="true" />
            <span>{step.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

interface Props {
  entries: Entry[];
  results: Record<string, string>;
  reports: string[];
  empty: string;
  onRespond: (
    id: string,
    approved: boolean,
    command: string,
    remember: "session" | "project" | null,
  ) => Promise<void>;
}

export function Chat({ entries, results, reports, empty, onRespond }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  // Follow the bottom, but stop following the moment the reader scrolls up:
  // a transcript that yanks itself back down while you are reading a tool
  // result is a transcript you cannot read during a long run.
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [entries, results, reports]);

  return (
    <div
      className="chat-scroll"
      ref={scrollRef}
      onScroll={(e) => {
        const el = e.currentTarget;
        pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      }}
    >
      <div className="chat-col">
        {entries.length === 0 && <div className="chat-empty">{empty}</div>}
        {entries.map((entry) => {
          switch (entry.kind) {
            case "item":
              return <Item key={entry.key} item={entry.item} results={results} />;
            case "note":
              return (
                <div key={entry.key} className={`msg-note${entry.tone === "danger" ? " danger" : ""}`}>
                  {entry.text}
                </div>
              );
            case "mark":
              return <Mark key={entry.key} mark={entry.mark} payload={entry.payload} />;
            case "approval":
              return <ApprovalCard key={entry.key} entry={entry} onRespond={onRespond} />;
          }
        })}
        {reports.length > 0 && (
          <div className="run-reports">
            {reports.map((line, i) => (
              <div key={i}>[{line}]</div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
```

> - `Chat` 里的 `useEffect`：每次内容变了，如果用户本来就在底部，就滚到底部；用户往上翻了，就不再自动往下拉（注释讲了为什么）。
> - `entries.map((entry) => { switch (entry.kind) ... })`：每种条目用对应的组件画，`key={entry.key}`。

### 7.7 代码：`App.tsx`——整个页面

状态和启动：

```tsx
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type PolicyInfo } from "./api";
import { Chat, PlanPanel } from "./components/Chat";
import { Extensions } from "./components/Extensions";
import { Sidebar } from "./components/Sidebar";
import { ThreadSettingsForm } from "./components/ThreadSettings";
import type { McpServer, Provider, Thread, ThreadSettings, WorkspaceGroup } from "./types";
import { useThread } from "./useThread";

export default function App() {
  const [policy, setPolicy] = useState<PolicyInfo | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceGroup[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [servers, setServers] = useState<McpServer[]>([]);
  const [threadId, setThreadId] = useState<string | null>(null);
  const [thread, setThread] = useState<Thread | null>(null);
  const [view, setView] = useState<"chat" | "extensions">("chat");
  const [showSettings, setShowSettings] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [modelMenu, setModelMenu] = useState(false);
  const [booted, setBooted] = useState(false);

  const live = useThread(threadId);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const reloadWorkspaces = useCallback(async () => {
    setWorkspaces(await api.workspaces());
  }, []);
  const reloadProviders = useCallback(async () => {
    setProviders(await api.providers());
  }, []);
  const reloadMcp = useCallback(async () => {
    setServers(await api.mcp());
  }, []);

  useEffect(() => {
    void (async () => {
      const [pol, spaces, provs, mcp] = await Promise.all([
        api.policy(),
        api.workspaces(),
        api.providers(),
        api.mcp(),
      ]);
      setPolicy(pol);
      setWorkspaces(spaces);
      setProviders(provs);
      setServers(mcp);
      // Land somewhere useful rather than on an empty chat.
      const first = spaces.find((w) => w.threads.length > 0)?.threads[0];
      if (first) setThreadId(first.id);
      setBooted(true);
    })().catch(() => setBooted(true));
  }, []);

  useEffect(() => {
    if (!threadId) {
      setThread(null);
      return;
    }
    void api.thread(threadId).then(setThread).catch(() => setThread(null));
  }, [threadId]);

  // The sidebar's per-thread metadata (item counts, busy dots) is derived from
  // files the turn writes, so it is refreshed when a turn ends rather than on
  // a timer.
  useEffect(() => {
    if (!live.busy) void reloadWorkspaces().catch(() => undefined);
  }, [live.busy, reloadWorkspaces]);
```

> - 一大串 `useState`：当前打开哪个会话、在聊天页还是扩展页、设置抽屉开没开、输入框里的草稿……
> - `useThread(threadId)`：§7.5 的那个 hook，`live` 就是它返回的实时视图。
> - 第一个 `useEffect`（依赖是 `[]`，只在第一次画完后执行一次）：同时拉策略、工作区、提供方、MCP 服务器四样东西（`Promise.all`），再自动打开第一个有会话的工作区里的第一个会话。
> - 第二个：换了会话就拉一次这个会话的记录（标题、设置）。
> - 第三个：一轮结束（`busy` 变成 `false`）时刷新侧边栏——条目数、"忙"的小圆点都是从文件里算的。

几个动作：

```tsx
const active = providers.find((p) => p.active) ?? null;
const ready = Boolean(active && threadId);

const openThread = (id: string) => {
  setThreadId(id);
  setView("chat");
  setShowSettings(false);
  setSidebarOpen(false);
};

const newSession = async (workspace: string) => {
  const created = await api.createThread(workspace, "New session", policy?.defaults ?? {});
  await reloadWorkspaces();
  openThread(created.id);
};

const send = async () => {
  const text = draft.trim();
  if (!text || !ready || live.busy) return;
  setDraft("");
  if (textareaRef.current) textareaRef.current.style.height = "auto";
  await live.send(text);
};

const saveSettings = async (next: ThreadSettings) => {
  if (!threadId) return;
  const updated = await api.patchThread(threadId, { settings: next });
  setThread(updated);
  await reloadWorkspaces();
};

if (!booted) return <div className="boot">starting…</div>;
```

> - `ready`：有启用的提供方、也打开了一个会话，才能发消息。
> - `newSession(workspace)`：在这个工作区建一个新会话，用策略接口给的默认设置。
> - `send()`：清空输入框，交给 `live.send`。

扩展页：

```tsx
if (view === "extensions") {
  return (
    <div className="app">
      <Sidebar
        workspaces={workspaces}
        currentThreadId={threadId}
        onOpenThread={openThread}
        onAddWorkspace={newSession}
        onNewSession={newSession}
        onOpenExtensions={() => setView("extensions")}
        open={sidebarOpen}
      />
      <main className="main">
        <Extensions
          onBack={() => setView("chat")}
          threadId={threadId}
          providers={providers}
          servers={servers}
          reloadProviders={reloadProviders}
          reloadMcp={reloadMcp}
        />
      </main>
    </div>
  );
}
```

聊天页：

```tsx
  return (
    <div className="app">
      {sidebarOpen && <div className="scrim show" onClick={() => setSidebarOpen(false)} />}
      <Sidebar
        workspaces={workspaces}
        currentThreadId={threadId}
        onOpenThread={openThread}
        onAddWorkspace={newSession}
        onNewSession={newSession}
        onOpenExtensions={() => setView("extensions")}
        open={sidebarOpen}
      />

      <main className="main">
        <div className="mobile-bar">
          <button className="hamburger" aria-label="Open sidebar" onClick={() => setSidebarOpen(true)}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M3 6h18M3 12h18M3 18h18" />
            </svg>
          </button>
          <span className="mb-title">minicodex</span>
        </div>

        <section className="view active">
          <div className="chat-header">
            <div>
              <div className="chat-title">{thread?.title ?? "No session open"}</div>
              <div className="chat-path">{thread?.workspace ?? "Add a workspace to start"}</div>
            </div>
            {thread && (
              <div className="chat-header-right">
                {/* The permission state, permanently on screen. `live.mode`
                    tracks a mid-turn `request_permissions` escalation, so this
                    shows what is true now, not what was chosen at creation. */}
                <span className={`mode-chip mode-${live.mode ?? thread.settings.sandbox_mode}`}>
                  {live.mode ?? thread.settings.sandbox_mode} · {thread.settings.approval_policy}
                </span>
                <span className={`conn-chip conn-${live.connection}`} title={live.connection}>
                  {live.connection === "open" ? "live" : live.connection}
                </span>
                <button className="btn btn-ghost btn-sm" onClick={() => setShowSettings((v) => !v)}>
                  Session settings
                </button>
              </div>
            )}
          </div>

          {showSettings && thread && policy && (
            <div className="settings-drawer">
              <ThreadSettingsForm
                policy={policy}
                value={thread.settings}
                disabled={live.busy}
                onChange={(next) => void saveSettings(next)}
              />
              {live.busy && (
                <p className="settings-help">
                  Settings are read once, when a turn starts. They are locked while this one runs.
                </p>
              )}
            </div>
          )}

          <div className="chat-body">
            <Chat
              entries={live.entries}
              results={live.results}
              reports={live.reports}
              onRespond={live.respond}
              empty={
                threadId
                  ? "This session has no messages yet. Say what you want done."
                  : "Add a workspace on the left, then send the first message. minicodex will show its work as it goes -- tool calls, results, and anything it needs to ask you about."
              }
            />
            <PlanPanel steps={live.plan} />
          </div>

          <div className="composer">
            <div className="composer-col">
              {modelMenu && (
                <div className="popover popover-right show">
                  <div className="pop-label">Switch model</div>
                  <div>
                    {providers.map((p) => (
                      <button
                        key={p.id}
                        className={`pop-row${p.active ? " on" : ""}`}
                        onClick={async () => {
                          await api.activateProvider(p.id);
                          await reloadProviders();
                          setModelMenu(false);
                        }}
                      >
                        <span>{p.model}</span>
                        <span className="pop-sub">{p.name}</span>
                      </button>
                    ))}
                  </div>
                </div>
              )}

              <div className={`composer-box${live.busy ? " disabled" : ""}`}>
                <textarea
                  ref={textareaRef}
                  rows={1}
                  disabled={!ready}
                  value={draft}
                  placeholder="Ask minicodex to fix something. It will show its work."
                  onChange={(e) => {
                    setDraft(e.target.value);
                    e.target.style.height = "auto";
                    e.target.style.height = `${Math.min(e.target.scrollHeight, 160)}px`;
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                />
                <button
                  className="model-btn"
                  aria-expanded={modelMenu}
                  onClick={() => setModelMenu((v) => !v)}
                >
                  <span>{active ? active.model : "no provider"}</span>
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M6 9l6 6 6-6" />
                  </svg>
                </button>
                {live.busy ? (
                  <button className="btn btn-danger btn-sm" onClick={() => void live.stop()}>
                    Stop
                  </button>
                ) : (
                  <button
                    className="btn btn-primary btn-sm"
                    disabled={!ready || !draft.trim()}
                    onClick={() => void send()}
                  >
                    Send
                  </button>
                )}
              </div>

              <div className="composer-meta">
                <span>Shift+Enter for a new line</span>
                <span className={live.error ? "danger" : ""}>
                  {live.error ?? (live.busy ? "thinking…" : "")}
                </span>
              </div>
            </div>
          </div>
        </section>
      </main>
    </div>
  );
}
```

### 7.8 代码：`Sidebar.tsx`

```tsx
import { useState } from "react";
import type { WorkspaceGroup } from "../types";

interface Props {
  workspaces: WorkspaceGroup[];
  currentThreadId: string | null;
  onOpenThread: (id: string) => void;
  onAddWorkspace: (path: string) => Promise<void>;
  onNewSession: (path: string) => Promise<void>;
  onOpenExtensions: () => void;
  open: boolean;
}

/**
 * Sessions grouped by working directory, not a flat list.
 *
 * Two separate `+` buttons, deliberately: the one next to the "Workspaces"
 * label adds a workspace, the one on each group header starts a session in
 * *that* workspace. Collapsing them into one button makes the common action
 * ("another session here") ambiguous with the rare one.
 */
export function Sidebar({
  workspaces,
  currentThreadId,
  onOpenThread,
  onAddWorkspace,
  onNewSession,
  onOpenExtensions,
  open,
}: Props) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [adding, setAdding] = useState(false);
  const [path, setPath] = useState("");
  const [error, setError] = useState("");

  const submit = async () => {
    const trimmed = path.trim();
    if (!trimmed) return;
    try {
      await onAddWorkspace(trimmed);
      setPath("");
      setAdding(false);
      setError("");
    } catch (exc) {
      setError((exc as Error).message);
    }
  };

  return (
    <aside className={`sidebar${open ? " open" : ""}`}>
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none">
            <path
              d="M4 12L10 6M4 12L10 18M20 12L14 6M20 12L14 18"
              stroke="#FFFFFF"
              strokeWidth="2.4"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </span>
        <div>
          <div className="brand-word">minicodex</div>
          <div className="brand-tag">console · live</div>
        </div>
      </div>

      <div className="sidebar-section" style={{ flex: 1, minHeight: 0 }}>
        <div className="sidebar-label-row">
          <span className="sidebar-label">Workspaces</span>
          <button
            className="ws-add"
            aria-label="Add workspace"
            title="Add workspace"
            onClick={() => setAdding((v) => !v)}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 5v14M5 12h14" />
            </svg>
          </button>
        </div>

        <div className={`ws-add-form${adding ? " open" : ""}`}>
          <input
            value={path}
            spellCheck={false}
            placeholder="/absolute/path/to/project"
            onChange={(e) => setPath(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void submit();
              }
            }}
          />
          <button className="btn btn-primary btn-sm" onClick={() => void submit()}>
            Add
          </button>
        </div>
        {error && <div className="sidebar-error">{error}</div>}

        <div>
          {workspaces.length === 0 && (
            <div className="sidebar-empty">No workspaces yet -- click + above to add one.</div>
          )}
          {workspaces.map((ws) => {
            const isOpen = expanded[ws.path] !== false;
            return (
              <div className="ws-group" key={ws.path}>
                <div className="ws-header">
                  <button
                    className="ws-toggle"
                    aria-expanded={isOpen}
                    onClick={() => setExpanded((c) => ({ ...c, [ws.path]: !isOpen }))}
                  >
                    <svg
                      className={`ws-chevron${isOpen ? " open" : ""}`}
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.2"
                    >
                      <path d="M9 6l6 6-6 6" />
                    </svg>
                    <svg
                      className="ws-folder"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.6"
                    >
                      <path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z" />
                    </svg>
                    <span className="ws-path">{ws.path}</span>
                  </button>
                  <button
                    className="ws-add"
                    title="New session here"
                    onClick={(e) => {
                      e.stopPropagation();
                      setExpanded((c) => ({ ...c, [ws.path]: true }));
                      void onNewSession(ws.path);
                    }}
                  >
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M12 5v14M5 12h14" />
                    </svg>
                  </button>
                </div>
                <ul className="thread-list" style={{ display: isOpen ? "flex" : "none" }}>
                  {ws.threads.map((t) => (
                    <li
                      key={t.id}
                      className={`thread-item${t.id === currentThreadId ? " active" : ""}`}
                      onClick={() => onOpenThread(t.id)}
                    >
                      <span className="t-title">
                        {t.title}
                        {t.busy && <span className="t-busy" title="answering" />}
                      </span>
                      <span className="t-meta">
                        {t.model ? `${t.model} · ${t.message_count} items` : "no messages yet"}
                        {" · "}
                        {t.settings.sandbox_mode}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
        </div>
      </div>

      <div className="sidebar-footer">
        <button className="nav-item" onClick={onOpenExtensions}>
          <span className="n-label">
            <svg
              className="n-icon"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
            >
              <path d="M12 3v6M12 15v6M3 12h6M15 12h6" />
              <circle cx="12" cy="12" r="2.6" />
            </svg>
            Extensions
          </span>
        </button>
      </div>
    </aside>
  );
}
```

> - 按工作区分组，每组可以折叠。`expanded[ws.path] !== false`：没记录过的默认展开。
> - 两个 `+` 号：顶上那个加一个工作区（输入一个绝对路径），每组标题上那个在这个工作区新建一个会话。注释讲了为什么不合成一个。
> - 加工作区失败（路径不存在之类）时，把后端返回的那句话显示在输入框下面。
> - `e.stopPropagation()`：点组标题上的 `+` 时，不要让这次点击再传给外层、触发"折叠"。

### 7.9 代码：`Extensions.tsx` 和样式

扩展页有六个标签：提供方、MCP 服务器、技能、记忆、规则、插件。它们的形状都一样——拉一个接口、画一列卡片、有的带一个"添加"表单——所以这里逐段贴出来，只详细讲第一个。

```tsx
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { McpServer, MemoryEntry, Provider, Rule, Skill } from "../types";

type Tab = "provider" | "mcp" | "skills" | "memory" | "rules" | "plugins";

function StatusChip({ tone, text }: { tone: "ok" | "info" | "danger"; text: string }) {
  return (
    <span className={`status-chip status-${tone}`}>
      <span className="dot" />
      {text}
    </span>
  );
}

function Providers({ providers, reload }: { providers: Provider[]; reload: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    name: "",
    provider: "openai",
    base_url: "",
    model: "",
    api_key: "",
  });

  const save = async () => {
    setError("");
    if (!form.name.trim() || !form.base_url.trim() || !form.model.trim()) {
      setError("name, base URL and model are required");
      return;
    }
    try {
      await api.addProvider({
        name: form.name.trim(),
        provider: form.provider,
        base_url: form.base_url.trim(),
        model: form.model.trim(),
        api_key: form.api_key.trim() || null,
      });
    } catch (exc) {
      setError((exc as Error).message);
      return;
    }
    setForm({ name: "", provider: "openai", base_url: "", model: "", api_key: "" });
    setOpen(false);
    await reload();
  };

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "46ch" }}>
          minicodex talks to one provider at a time. Add every model you use and switch the active
          one from here or from the chat input.
        </p>
        <button className="btn btn-sm" onClick={() => setOpen((v) => !v)}>
          + Add provider
        </button>
      </div>

      <div className={`add-form${open ? " open" : ""}`}>
        <div className="form-grid">
          <div className="form-row">
            <label htmlFor="p-name">Name</label>
            <input
              id="p-name"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </div>
          <div className="form-row">
            <label htmlFor="p-kind">Kind</label>
            <select
              id="p-kind"
              value={form.provider}
              onChange={(e) => setForm({ ...form, provider: e.target.value })}
            >
              <option value="openai">openai-compatible</option>
              <option value="ollama">ollama</option>
            </select>
          </div>
        </div>
        <div className="form-grid">
          <div className="form-row">
            <label htmlFor="p-url">Base URL</label>
            <input
              id="p-url"
              placeholder="https://api.example.com/v1"
              value={form.base_url}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            />
          </div>
          <div className="form-row">
            <label htmlFor="p-model">Model</label>
            <input
              id="p-model"
              placeholder="e.g. gpt-4o-mini"
              value={form.model}
              onChange={(e) => setForm({ ...form, model: e.target.value })}
            />
          </div>
        </div>
        <div className="form-row">
          <label htmlFor="p-key">API key</label>
          <input
            id="p-key"
            type="password"
            placeholder="Leave blank for a local provider or to use $OPENAI_API_KEY"
            value={form.api_key}
            onChange={(e) => setForm({ ...form, api_key: e.target.value })}
          />
        </div>
        <div className="form-actions">
          <span className="form-error">{error}</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>
            Cancel
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => void save()}>
            Save provider
          </button>
        </div>
      </div>

      <div className="card-list">
        {providers.map((p) => (
          <div className="item-card" key={p.id}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{p.name}</span>
                {p.active && <span className="ic-active-tag">Active</span>}
              </div>
              <div className="ic-meta">
                {p.model} · {p.base_url}
              </div>
            </div>
            <div className="ic-actions">
              <StatusChip
                tone="ok"
                text={
                  p.api_key
                    ? "Key set"
                    : p.provider === "ollama"
                      ? "No key needed"
                      : "Using $OPENAI_API_KEY"
                }
              />
              {!p.active && (
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={async () => {
                    await api.activateProvider(p.id);
                    await reload();
                  }}
                >
                  Set active
                </button>
              )}
              <button
                className="btn btn-ghost btn-sm"
                onClick={async () => {
                  await api.deleteProvider(p.id);
                  await reload();
                }}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

> - `StatusChip`：一个带颜色小圆点的标签。
> - `Providers`：`form` 是表单的五个字段；`save` 检查必填项、调 `api.addProvider`、失败就显示错误、成功就清空表单并刷新列表。列表里每个提供方一张卡片，可以设为当前、可以删除。key 只显示"有没有"（后端从不把 key 发回浏览器，见 `routes._public`）。

MCP 服务器（同样的形状，多了环境变量、工作目录、两个超时）：

```tsx
function Mcp({ servers, reload }: { servers: McpServer[]; reload: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    name: "",
    command: "",
    env: "",
    cwd: "",
    startup_timeout: "30",
    tool_timeout: "60",
  });

  const save = async () => {
    setError("");
    if (!form.name.trim() || !form.command.trim()) {
      setError("name and command are required");
      return;
    }
    // `KEY=value` per line -- the shape a person already knows from a shell,
    // and the shape `ServerConfig.env` wants.
    const env: Record<string, string> = {};
    for (const line of form.env.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const eq = trimmed.indexOf("=");
      if (eq <= 0) {
        setError(`env line is not KEY=value: ${trimmed}`);
        return;
      }
      env[trimmed.slice(0, eq)] = trimmed.slice(eq + 1);
    }
    try {
      await api.addMcp({
        name: form.name.trim(),
        command: form.command.trim(),
        env,
        cwd: form.cwd.trim() || null,
        startup_timeout: Number(form.startup_timeout),
        tool_timeout: Number(form.tool_timeout),
      });
    } catch (exc) {
      setError((exc as Error).message);
      return;
    }
    setForm({ name: "", command: "", env: "", cwd: "", startup_timeout: "30", tool_timeout: "60" });
    setOpen(false);
    await reload();
  };

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "46ch" }}>
          Outside tool servers minicodex can call over MCP. Each server's tools load under its own
          namespace, so two servers can both offer <code>search</code>.
        </p>
        <button className="btn btn-sm" onClick={() => setOpen((v) => !v)}>
          + Add server
        </button>
      </div>

      <div className={`add-form${open ? " open" : ""}`}>
        <div className="form-row">
          <label htmlFor="m-name">Name</label>
          <input
            id="m-name"
            placeholder="e.g. postgres"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </div>
        <div className="form-row">
          <label htmlFor="m-cmd">Command</label>
          <input
            id="m-cmd"
            placeholder="npx @org/mcp-server-name --flag"
            value={form.command}
            onChange={(e) => setForm({ ...form, command: e.target.value })}
          />
        </div>
        {/* env / cwd / the two timeouts were readable by the backend from the
            start and settable by nobody, so every server ran with the
            defaults for ever. */}
        <div className="form-row">
          <label htmlFor="m-env">Environment</label>
          <textarea
            id="m-env"
            rows={3}
            placeholder={"KEY=value, one per line"}
            value={form.env}
            onChange={(e) => setForm({ ...form, env: e.target.value })}
          />
        </div>
        <div className="form-grid">
          <div className="form-row">
            <label htmlFor="m-cwd">Working directory</label>
            <input
              id="m-cwd"
              placeholder="optional"
              value={form.cwd}
              onChange={(e) => setForm({ ...form, cwd: e.target.value })}
            />
          </div>
          <div className="form-row">
            <label htmlFor="m-start">Startup timeout (s)</label>
            <input
              id="m-start"
              value={form.startup_timeout}
              onChange={(e) => setForm({ ...form, startup_timeout: e.target.value })}
            />
          </div>
        </div>
        <div className="form-row">
          <label htmlFor="m-tool">Tool call timeout (s)</label>
          <input
            id="m-tool"
            value={form.tool_timeout}
            onChange={(e) => setForm({ ...form, tool_timeout: e.target.value })}
          />
        </div>
        <div className="form-actions">
          <span className="form-error">{error}</span>
          <button className="btn btn-ghost btn-sm" onClick={() => setOpen(false)}>
            Cancel
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => void save()}>
            Add server
          </button>
        </div>
      </div>

      <div className="card-list">
        {servers.map((m) => (
          <div className="item-card" key={m.id}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{m.name}</span>
              </div>
              <div className="ic-meta">{m.command.join(" ")}</div>
            </div>
            <div className="ic-actions">
              <button
                className={`switch${m.enabled ? " on" : ""}`}
                role="switch"
                aria-checked={m.enabled}
                onClick={async () => {
                  await api.toggleMcp(m.id, !m.enabled);
                  await reload();
                }}
              >
                <span className="knob" />
              </button>
              <button
                className="btn btn-ghost btn-sm"
                onClick={async () => {
                  await api.deleteMcp(m.id);
                  await reload();
                }}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

技能和记忆：

```tsx
function Skills({ threadId }: { threadId: string | null }) {
  const [data, setData] = useState<{
    directory: string;
    describe: string;
    skipped: string[];
    items: Skill[];
  } | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    if (!threadId) return;
    void api.skills(threadId).then(setData).catch(() => setData(null));
  }, [threadId]);

  if (!threadId) {
    return (
      <div className="ext-panel active">
        <div className="ext-empty">
          <div className="ee-title">Open a session first</div>
          Skills are discovered per workspace, so this needs to know which one.
        </div>
      </div>
    );
  }

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "52ch" }}>
          A skill is a directory with a <code>SKILL.md</code> in it. Only the name and one line of
          description ride in every request; the body is fetched by the model with{" "}
          <code>read_skill</code> when it decides it wants it.
        </p>
      </div>
      <p className="ic-desc">
        <code>{data?.directory}</code> — {data?.describe}
      </p>
      {data && data.skipped.length > 0 && (
        <p className="form-error">skipped: {data.skipped.join(", ")}</p>
      )}
      <div className="card-list">
        {(data?.items ?? []).map((skill) => (
          <div className="item-card skill-card" key={skill.name}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{skill.name}</span>
              </div>
              <div className="ic-meta">{skill.description}</div>
              {open === skill.name && <pre className="skill-body">{skill.body}</pre>}
            </div>
            <div className="ic-actions">
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => setOpen(open === skill.name ? null : skill.name)}
              >
                {open === skill.name ? "Hide" : "Read"}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Memory({ threadId }: { threadId: string | null }) {
  const [data, setData] = useState<{
    directory: string;
    summary?: string;
    describe?: string;
    error?: string;
    entries: MemoryEntry[];
  } | null>(null);
  const [jobs, setJobs] = useState<Record<string, number>>({});
  const [confirming, setConfirming] = useState(false);

  const load = useCallback(async () => {
    if (!threadId) return;
    setData(await api.memory(threadId));
    setJobs((await api.memoryJobs(threadId)).counts);
  }, [threadId]);

  useEffect(() => {
    void load().catch(() => undefined);
  }, [load]);

  if (!threadId) {
    return (
      <div className="ext-panel active">
        <div className="ext-empty">
          <div className="ee-title">Open a session first</div>
          Memory lives in the workspace, so this needs to know which one.
        </div>
      </div>
    );
  }

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "52ch" }}>
          What this project has learned about itself. Switch reading and writing on per session
          under <strong>Session settings</strong>; both are off until you say otherwise.
        </p>
        <button className="btn btn-danger btn-sm" onClick={() => setConfirming(true)}>
          Erase everything
        </button>
      </div>

      {confirming && (
        <div className="confirm-box">
          <div className="confirm-title">Erase all memories for this workspace?</div>
          <p>
            Deletes <code>MEMORY.md</code>, the summary and the usage counts, and clears the
            pending write queue. It cannot be undone from here.
          </p>
          <div className="ac-actions">
            <button
              className="btn btn-danger btn-sm"
              onClick={async () => {
                await api.forgetAll(threadId);
                setConfirming(false);
                await load();
              }}
            >
              Erase
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      <p className="ic-desc">
        <code>{data?.directory}</code> — {data?.error ?? data?.describe}
      </p>
      {Object.keys(jobs).length > 0 && (
        <p className="ic-desc">
          write queue:{" "}
          {Object.entries(jobs)
            .map(([state, n]) => `${state} ${n}`)
            .join(" · ")}
        </p>
      )}

      {data?.summary && <pre className="memory-summary">{data.summary}</pre>}

      <div className="card-list">
        {(data?.entries ?? []).map((entry) => (
          <div className="item-card" key={entry.id}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{entry.title}</span>
              </div>
              <div className="ic-meta">
                {entry.lines} line(s)
                {entry.usage?.uses ? ` · cited ${entry.usage.uses}x` : " · never cited"}
              </div>
            </div>
            <div className="ic-actions">
              <StatusChip tone={entry.usage?.uses ? "ok" : "info"} text={entry.id} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
```

> 记忆面板里写着"这个工作区的记忆"，后端读的也是工作区下面的目录——而每一轮对话读的是 `~/.minicodex/memories`。§10.4 讲这件事，以及为什么这里不修。

规则、整个扩展页：

```tsx
function Rules({ threadId }: { threadId: string | null }) {
  const [rules, setRules] = useState<Rule[]>([]);

  const load = useCallback(async () => {
    if (!threadId) return;
    setRules(await api.rules(threadId));
  }, [threadId]);

  useEffect(() => {
    void load().catch(() => undefined);
  }, [load]);

  if (!threadId) {
    return (
      <div className="ext-panel active">
        <div className="ext-empty">
          <div className="ee-title">Open a session first</div>
          Remembered approvals belong to a workspace.
        </div>
      </div>
    );
  }

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "52ch" }}>
          Every &ldquo;always allow&rdquo; you have clicked, and the command that prompted it. A
          rule you cannot find is a rule you cannot take back, which is why this tab exists at all.
        </p>
      </div>
      <div className="card-list">
        {rules.length === 0 && (
          <div className="ext-empty">
            <div className="ee-title">Nothing remembered</div>
            Approving with &ldquo;Always in this project&rdquo; puts a rule here.
          </div>
        )}
        {rules.map((rule) => (
          <div className="item-card" key={rule.index}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{rule.words.join(" ")}</span>
                <span className="ic-active-tag">{rule.scope}</span>
              </div>
              <div className="ic-meta">
                prompted by: {rule.prompted_by} · {rule.created_at}
              </div>
            </div>
            <div className="ic-actions">
              <button
                className="btn btn-danger btn-sm"
                onClick={async () => {
                  await api.revokeRule(threadId, rule.index);
                  await load();
                }}
              >
                Revoke
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

interface Props {
  onBack: () => void;
  threadId: string | null;
  providers: Provider[];
  servers: McpServer[];
  reloadProviders: () => Promise<void>;
  reloadMcp: () => Promise<void>;
}
```

```tsx
export function Extensions({
  onBack,
  threadId,
  providers,
  servers,
  reloadProviders,
  reloadMcp,
}: Props) {
  const [tab, setTab] = useState<Tab>("provider");
  const tabs: { id: Tab; label: string; count?: number | string }[] = [
    { id: "provider", label: "Providers", count: providers.length },
    { id: "mcp", label: "MCP servers", count: servers.length },
    { id: "skills", label: "Skills" },
    { id: "memory", label: "Memory" },
    { id: "rules", label: "Rules" },
    { id: "plugins", label: "Plugins", count: "--" },
  ];

  return (
    <section className="view active">
      <div className="ext-shell">
        <div className="ext-header">
          <button className="btn btn-ghost btn-sm ext-back" onClick={onBack}>
            ← Back to chat
          </button>
          <h1 className="ext-title">Extensions</h1>
          <p className="ext-sub">
            Everything minicodex can reach beyond its own code: which model answers, which outside
            tools it can call, and what it has learned to do without being told twice.
          </p>
        </div>

        <div className="ext-tabs">
          {tabs.map((t) => (
            <button
              key={t.id}
              className={`ext-tab${tab === t.id ? " active" : ""}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
              {t.count !== undefined && <span className="tab-count">{t.count}</span>}
            </button>
          ))}
        </div>

        <div className="ext-body">
          {tab === "provider" && <Providers providers={providers} reload={reloadProviders} />}
          {tab === "mcp" && <Mcp servers={servers} reload={reloadMcp} />}
          {tab === "skills" && <Skills threadId={threadId} />}
          {tab === "memory" && <Memory threadId={threadId} />}
          {tab === "rules" && <Rules threadId={threadId} />}
          {tab === "plugins" && (
            <div className="ext-panel active">
              <div className="ext-empty">
                <div className="ee-title">Not available yet</div>
                minicodex has no plugin or bundle mechanism to install from here — codex does, this
                does not, and an empty list would imply this tab had looked and found nothing.
                Reserved for when one exists.
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
```

> 插件标签明说"没有这个东西"，而不是显示一个空列表——空列表会让人以为它找过、没找到。

样式有两个文件。`theme.css`（627 行）是上一版控制台的设计——颜色、字体、间距这些"设计令牌"和整体布局——经过两轮评审定下来，这一章**一行没改**，原样搬过来；它不是这一章写的代码，从快照里复制即可。`console.css` 是这一章新写的，全部用 `theme.css` 里的变量，没有一个写死的颜色：

```css
/* Chapter 19's additions to the design system in theme.css.
 *
 * Kept in a second file rather than merged in, so that `git diff` against the
 * reviewed original shows nothing: everything the console looked like before
 * is still exactly what theme.css says, and everything new is here.
 *
 * Every colour is a token from theme.css's :root. Nothing below hardcodes a
 * hex value, which is what makes dark mode work without a second copy of any
 * of these rules.
 */

.boot {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--ink-faint);
  padding: 24px 32px;
}

.sidebar-error {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--danger);
  padding: 4px 8px 0;
  overflow-wrap: anywhere;
}

/* A quiet dot next to a thread that is answering, so a background turn in
   another thread is visible without opening it. */
.t-busy {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-left: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: t-pulse 1.4s ease-in-out infinite;
}
@keyframes t-pulse {
  0%, 100% { opacity: 0.25; }
  50% { opacity: 1; }
}

/* -- chat header: what this session is allowed to do, always visible ------- */

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
}
.chat-header-right {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  flex-wrap: wrap;
}

.mode-chip,
.conn-chip {
  font-family: var(--font-mono);
  font-size: 10.5px;
  letter-spacing: 0.04em;
  padding: 3px 9px;
  border-radius: var(--r-pill);
  border: 1px solid var(--border);
  white-space: nowrap;
}
.mode-chip { background: var(--surface); color: var(--ink-dim); }
/* Escalating colour with escalating permission: `full-access` should not look
   like a neutral setting, because it is not one. */
.mode-read-only { border-color: var(--border); }
.mode-workspace-write { color: var(--info); border-color: var(--info); background: var(--info-soft); }
.mode-full-access { color: var(--danger); border-color: var(--danger); background: var(--danger-soft); }

.conn-open { color: var(--ok); border-color: var(--ok); background: var(--ok-soft); }
.conn-connecting, .conn-retrying { color: var(--info); border-color: var(--info); background: var(--info-soft); }
.conn-closed { color: var(--ink-faint); }

/* -- chat body: transcript beside the plan -------------------------------- */

.chat-body {
  flex: 1;
  min-height: 0;
  display: flex;
  align-items: stretch;
}
.chat-body .chat-scroll { flex: 1; min-width: 0; }

.plan-panel {
  width: 250px;
  flex: none;
  border-left: 1px solid var(--border);
  background: var(--surface-2);
  padding: var(--sp-4);
  overflow-y: auto;
}
.plan-panel ul { list-style: none; margin: var(--sp-2) 0 0; padding: 0; display: flex; flex-direction: column; gap: var(--sp-2); }
.plan-step { display: flex; gap: var(--sp-2); font-size: 12.5px; line-height: 1.45; color: var(--ink-dim); }
.plan-box {
  width: 12px; height: 12px; flex: none; margin-top: 3px;
  border: 1.5px solid var(--ink-faint); border-radius: 3px;
}
.plan-in_progress { color: var(--ink); }
.plan-in_progress .plan-box { border-color: var(--accent); background: var(--accent-soft); }
.plan-completed { color: var(--ink-faint); text-decoration: line-through; }
.plan-completed .plan-box { border-color: var(--ok); background: var(--ok); }

@media (max-width: 1100px) {
  .plan-panel { display: none; }
}

/* -- marks: the three things the old console dropped ---------------------- */

.msg-mark {
  align-self: center;
  max-width: 90%;
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--info);
  background: var(--info-soft);
  border: 1px dashed var(--info);
  border-radius: var(--r-sm);
  padding: 6px 12px;
  text-align: center;
}

.run-reports {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink-faint);
  line-height: 1.7;
  border-top: 1px dashed var(--border);
  padding-top: var(--sp-3);
  overflow-wrap: anywhere;
}

.tc-spin {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent);
  margin-left: auto;
  animation: t-pulse 1.1s ease-in-out infinite;
}
.toolcall-head { cursor: pointer; }

/* -- session settings drawer --------------------------------------------- */

.settings-drawer {
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  padding: var(--sp-5) var(--sp-6);
  max-height: 52vh;
  overflow-y: auto;
}
.settings-form { display: flex; flex-direction: column; gap: var(--sp-5); max-width: 660px; }
.settings-group { display: flex; flex-direction: column; gap: var(--sp-2); }
.settings-label {
  font-family: var(--font-mono);
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: 0.09em;
  color: var(--ink-faint);
}
.settings-help { font-size: 12.5px; color: var(--ink-dim); margin: 0; line-height: 1.5; }
.settings-input {
  font-family: var(--font-mono);
  font-size: 12.5px;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  background: var(--surface);
  color: var(--ink);
  max-width: 260px;
}

.radio-row { display: flex; gap: var(--sp-2); flex-wrap: wrap; }
.radio-pill {
  font-family: var(--font-mono);
  font-size: 12px;
  padding: 6px 13px;
  border-radius: var(--r-pill);
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--ink-dim);
  cursor: pointer;
}
.radio-pill:hover:not(:disabled) { border-color: var(--accent); color: var(--ink); }
.radio-pill.on { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); }
.radio-pill:disabled { opacity: 0.5; cursor: not-allowed; }

.check-row {
  display: flex;
  gap: var(--sp-3);
  align-items: flex-start;
  font-size: 13px;
  line-height: 1.5;
  color: var(--ink-dim);
  cursor: pointer;
  padding: 5px 0;
}
.check-row input { margin-top: 3px; flex: none; accent-color: var(--accent); }
.check-row strong { color: var(--ink); }

.confirm-box {
  border: 1px solid var(--info);
  border-left-width: 3px;
  background: var(--info-soft);
  border-radius: var(--r-md);
  padding: var(--sp-4);
  margin-top: var(--sp-2);
}
.confirm-title { font-weight: 600; font-size: 13.5px; color: var(--ink); margin-bottom: var(--sp-2); }
.confirm-box p { font-size: 12.5px; line-height: 1.55; color: var(--ink-dim); margin: 0 0 var(--sp-2); }

/* -- extensions: skills, memory, rules ----------------------------------- */

.skill-body,
.memory-summary {
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: var(--surface-3);
  border-radius: var(--r-sm);
  padding: var(--sp-3);
  margin-top: var(--sp-3);
  max-height: 340px;
  overflow-y: auto;
}
.skill-card { align-items: flex-start; }

.pop-row {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  padding: 8px 11px;
  border: none;
  background: none;
  border-radius: var(--r-sm);
  cursor: pointer;
  font-size: 13px;
  color: var(--ink);
  text-align: left;
}
.pop-row:hover { background: var(--surface-2); }
.pop-row.on { background: var(--accent-soft); color: var(--accent); }
.pop-sub { font-family: var(--font-mono); font-size: 10.5px; color: var(--ink-faint); }

textarea.settings-input,
.add-form textarea {
  font-family: var(--font-mono);
  font-size: 12.5px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  background: var(--surface);
  color: var(--ink);
  resize: vertical;
  width: 100%;
}
```

> CSS 的基本形状：`选择器 { 属性: 值; }`。`.msg-mark` 选中所有 `class="msg-mark"` 的元素；`var(--accent)` 用 `theme.css` 里定义的变量；`@media (max-width: 860px) { ... }` 里的规则只在窄屏幕上生效。

### 7.10 看见它：在真的浏览器里用一次

上面所有的测试都把事件喂给一个列表，然后检查列表。**没有一个测试问过：一个人在浏览器里看到了什么。** 这次改写给 `probe_web.py` 加了一段 `browser`，就问这件事：起一个假模型服务器（`minicodex.stub`，回放录好的回答）和控制台，用 Playwright 打开页面，发两条消息，数屏幕上有几个"你"、几个"minicodex"、几个工具块；再刷新一次，数一遍。刷新是从磁盘上的 rollout 文件重建的，所以它是标准答案：**实时画出来的应该和它一样。**

```python
_DRIVER = r"""
// Two messages in one thread; count what is on screen, live and after a reload.
const { chromium } = require("playwright");
(async () => {
  const base = process.argv[2];
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto(base);
  await page.click(".thread-item");
  const count = async () => [
    await page.locator(".msg-user").count(),
    await page.locator(".msg-agent").count(),
    await page.locator(".toolcall").count(),
  ];
  const rows = [];
  for (const question of ["what does this define?", "and again?"]) {
    await page.locator("textarea").fill(question);
    await page.locator("textarea").press("Enter");
    await page.waitForTimeout(300);
    await page.waitForSelector(".composer-box:not(.disabled)", { timeout: 20000 });
    await page.waitForTimeout(500);
    rows.push(["live, after " + JSON.stringify(question), ...(await count())]);
  }
  await page.reload();
  await page.click(".thread-item");
  await page.waitForTimeout(1500);
  rows.push(["after a reload", ...(await count())]);
  console.log(JSON.stringify(rows));
  await browser.close();
})();
"""


def browser() -> None:
    """Drive the built console in a real browser, against the stub model.

    Added when chapter 19 was rewritten, because the three faults it found in
    the live view were invisible from pytest: every test fed events to a list,
    and none of them asked what a person sees after a second message. A reload
    rebuilds the page from the rollout file, so it is the reference: the live
    view should agree with it. Needs Node, Playwright and `npm run build`.
    """
    import socket
    import subprocess
    import tempfile
    import threading
    from http.server import HTTPServer

    import httpx
    import uvicorn

    from minicodex import stub
    from minicodex.web import create_app

    dist = ROOT / "frontend" / "dist"
    if not (dist / "index.html").exists():
        print("no frontend build: npm --prefix frontend ci && npm --prefix frontend run build")
        return

    def free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    stub_port, web_port = free_port(), free_port()
    model_server = HTTPServer(("127.0.0.1", stub_port), stub._Handler)
    threading.Thread(target=model_server.serve_forever, daemon=True).start()
    scratch = Path(tempfile.mkdtemp())
    (scratch / "ws").mkdir()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(scratch / "console", frontend=dist),
            host="127.0.0.1",
            port=web_port,
            log_level="warning",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    base = f"http://127.0.0.1:{web_port}"
    for _ in range(100):
        try:
            httpx.get(f"{base}/api/policy")
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    provider = httpx.post(
        f"{base}/api/providers",
        json={
            "name": "stub",
            "provider": "ollama",
            "base_url": f"http://127.0.0.1:{stub_port}/v1",
            "model": "m",
        },
    ).json()
    httpx.post(f"{base}/api/providers/{provider['id']}/activate")
    httpx.post(f"{base}/api/threads", json={"workspace": str(scratch / "ws"), "title": "probe"})

    driver = scratch / "driver.cjs"
    driver.write_text(_DRIVER, encoding="utf-8")
    try:
        out = subprocess.run(
            ["node", str(driver), base], capture_output=True, text=True, timeout=120, check=True
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"could not drive a browser: {exc}")
        return
    finally:
        server.should_exit = True
        model_server.shutdown()
    print(f"  {'':<38}{'you':>5}{'agent':>7}{'tool calls':>12}")
    for label, you, agent, tools in json.loads(out.stdout):
        print(f"  {label:<38}{you:>5}{agent:>7}{tools:>12}")
```

> - `_DRIVER`：一段 JavaScript，写到临时文件里用 `node` 运行。`page.click(".thread-item")` 点开会话；`fill` 填输入框，`press("Enter")` 发送；`waitForSelector(".composer-box:not(.disabled)")` 等到输入框重新可用——也就是这一轮结束；`page.locator(".msg-user").count()` 数有几个用户消息气泡。
> - `browser()`：两个服务器都在后台线程里起；`httpx` 通过 API 加一个指向假模型的提供方、建一个会话；然后运行 `_DRIVER`，把它打印的 JSON 变成一张表。

```
$ uv run python probe_web.py browser       # 第一版的代码
                                          you  agent  tool calls
  live, after "what does this define?"      2      2           1
  live, after "and again?"                  5      5           2
  after a reload                            2      3           1
```

刷新之后是 2、3、1——对的：两条用户消息，三条助手消息（第一轮一条带工具调用、一条回答，第二轮一条回答），一个工具块。实时画出来的是 2、2、1 和 5、5、2。

### 7.11 改写时发现：第二条消息，把第一轮又画了一遍

**第一个问题：服务器把整段历史又推了一遍。** 先看服务器发了什么。同一个会话跑两轮，数每一轮发出的 `history_item` 事件：

```
turn 'what does this define?': 5 history_item events: ['system_note', 'user', 'assistant', 'tool_result', 'assistant']
turn 'and again?': 7 history_item events: ['system_note', 'user', 'assistant', 'tool_result', 'assistant', 'user', 'assistant']
```

第二轮发了 7 条，前 5 条是第一轮的原样重复。原因在 `agent.py` 里：续接时，Agent 用 `_attach` 把旧历史**逐条重放**进这一轮的新 rollout 文件（第 7 章：每个文件都是一份完整的对话）；而重放走的就是 `History` 的观察者——也就是 `StreamingRolloutWriter.append`，§2.2 那个"写进文件就推给浏览器"的钩子。对写入器来说，重放的旧条目和新条目长得一模一样。压缩（第 6 章）写新基线时也是这样。

§2.2 说"一个事件到达浏览器，当且仅当它也已经写到磁盘"——这条性质依然成立，**可写到磁盘的不全是新东西**。

修法不改 Agent：给每个事件编一个号。`StreamingRolloutWriter` 数着自己写了多少条，每个 `history_item` 带上 `index`——它在这个文件**读回来时**的历史里排第几（§9.1 的代码）；遇到 `compacting` 标记从 0 重新数，因为第 7 章的 `read_rollout` 读回来的正是最后一个 `compacting` 之后的部分。浏览器记着"屏幕上已经有几条"（`knownRef`），编号比它小的就跳过。

**第二个问题：乐观回显只认"最后一条"。** 第一条消息也被画了两次。`send` 先在屏幕上画出用户的消息；服务器那条 `user` 事件到了，第一版只在"最后一项恰好是同样文字的用户消息"时才不重复画。可一轮的第一条事件是系统提示（`system_note`），它排在回显后面——最后一项不再是回显，于是又画了一条。现在回显带一个 `pending` 标记，服务器的那条按文字去找它、替换它。

**第三个问题：第一次打开页面时，先拉快照、后订阅。** 第一版 `useEffect` 的主体是：

```typescript
    void resync(threadId)
      .then(() => open())
      .catch((exc: Error) => {
        setError(exc.message);
        open();
      });
```

先 `resync`，拉完再 `open`。`onopen` 里的注释写着"先订阅、再拉；反过来会留下一个缝，缝里的事件谁也不知道"——可它只在**重连**时这么做，每一次正常打开页面走的都是反过来的那条路。打开一个正在答题的会话，如果 `turn_finished` 恰好落在"拉完快照"和"连上 socket"之间，`busy` 就停在 `true`：输入框一直是禁用的，直到刷新。这一条是读代码读出来的，没有在浏览器里复现——要复现得让一个事件恰好落在几毫秒的窗口里。

修法：**每一次**连上（包括第一次）都先订阅、再拉快照；拉快照期间收到的事件先存进 `bufferRef`，快照到了再按顺序应用——编号让它们不会和快照重复。

**第四个问题：快照里没有审批。** 见 §4.3；前端这一半就是 `resync` 里那一行 `record.approvals`。

修完之后：

```
$ uv run python probe_web.py browser
                                          you  agent  tool calls
  live, after "what does this define?"      1      2           1
  live, after "and again?"                  2      3           1
  after a reload                            2      3           1
```

实时画出来的和刷新之后的一样了。

测试分两半。后端：编号和读回来的历史对得上；压缩时从 0 重新数：

```python
async def _two_turns(tmp_path: Path, stub_url: str) -> tuple[list[dict], list[dict], int]:
    """Two messages in one thread; the events of each, and how many items the
    thread loads back as after the first."""
    from minicodex.rollout import read_rollout, resolve
    from minicodex.web import runtime

    workspace = tmp_path / "ws"
    workspace.mkdir()
    batches: list[list[dict]] = []
    after_first = 0
    for question in ("what does this define?", "and again?"):
        events: list[dict] = []
        await runtime.run_turn(
            thread_dir=tmp_path / "thread",
            workspace_root=workspace,
            question=question,
            settings=dict(DEFAULT_THREAD_SETTINGS),
            provider_name="ollama",
            base_url=stub_url,
            model="m",
            api_key=None,
            mcp_servers=[],
            broker=ApprovalBroker(),
            emit=events.append,
        )
        batches.append([e for e in events if e["type"] == "history_item"])
        if not after_first:
            history, _ = read_rollout(resolve("last", tmp_path / "thread")).history()
            after_first = len(history.items)
    return batches[0], batches[1], after_first


async def test_every_streamed_item_says_where_it_sits_in_the_thread(
    tmp_path: Path, stub_url: str
) -> None:
    """A turn starts by replaying the conversation into its new rollout file,
    and that replay goes through the same `append` the browser listens to. The
    first version streamed it as if it were new, so the second message drew the
    first exchange on screen a second time -- 5 messages and 2 tool calls
    where a reload showed 2 and 1. The index is what lets the browser tell the
    replay from the new items; it has to agree with what a reload loads.
    """
    first, second, known = await _two_turns(tmp_path, stub_url)
    assert [e["index"] for e in first] == list(range(len(first)))
    assert known == len(first)
    # The second turn replays positions 0..known-1, then continues from there.
    assert [e["index"] for e in second] == list(range(len(second)))
    assert [e["item"] for e in second[:known]] == [e["item"] for e in first]
    assert [e["item"]["type"] for e in second[known:]] == ["user", "assistant"]


def test_a_compaction_numbers_its_new_baseline_from_zero(tmp_path: Path) -> None:
    """`read_rollout` keeps what follows the last `compacting` marker, so the
    count restarts there, and `compacted` says how long the new history is."""
    from minicodex.history import History
    from minicodex.rollout import SessionMeta, new_session_id, read_rollout
    from minicodex.web.events import StreamingRolloutWriter

    events: list[dict[str, Any]] = []
    meta = SessionMeta(
        session_id=new_session_id(),
        created=0.0,
        cwd=str(tmp_path),
        provider="ollama",
        model="m",
        sandbox_mode="read-only",
        approval_policy="on-request",
    )
    writer = StreamingRolloutWriter(tmp_path / "s.jsonl", meta, events.append)
    try:
        history = History(observer=writer.append)
        for text in ("one", "two", "three"):
            history.add_user(text)
        writer.mark("compacting", generation=1, replaced=2)
        baseline = History(observer=writer.append)
        baseline.add_system_note("summary")
        baseline.add_user("three")
        writer.mark("compacted", generation=1, replaced=2)
    finally:
        writer.release()

    assert [e["index"] for e in events if e["type"] == "history_item"] == [0, 1, 2, 0, 1]
    assert events[-1]["mark"] == "compacted" and events[-1]["position"] == 2
    loaded, _ = read_rollout(tmp_path / "s.jsonl").history()
    assert len(loaded.items) == 2
```

> - `_two_turns`：对着假模型跑两轮，返回两轮各自的 `history_item` 事件，和第一轮之后会话读回来有几条。
> - 第一个测试的关键断言：第二轮的前 `known` 条和第一轮**完全一样**、编号从 0 开始——它们是重放，浏览器应该跳过；后面两条是新的。
> - 第二个直接驱动 `StreamingRolloutWriter`：写三条、`compacting`、写两条新基线、`compacted`——编号是 0、1、2、0、1，`compacted` 说新历史有 2 条，`read_rollout` 读回来也正好 2 条。

前端：pytest 驱动不了浏览器（`probe_web.py browser` 量的就是这一半），所以和 F19-05 一样，检查源代码里有没有这几样东西：

```python
def test_F19_05_the_full_transcript_is_rebuildable_from_the_rollout_file(
    console: TestClient, workspace: Path
) -> None:
    """The frontend's reconnect is only survivable because the socket is not
    the only copy: `GET /api/threads/{id}` reads the same rollout file the
    events were derived from. This pins the endpoint that makes it possible.
    """
    thread_id = _thread(console, workspace)
    body = console.get(f"/api/threads/{thread_id}").json()
    assert "items" in body and "busy" in body and "settings" in body


def test_F19_05_the_frontend_reconnects_rather_than_only_listening() -> None:
    """The old console set `onmessage` and nothing else, so any dropped
    connection left the page reading `thinking…` for ever with no recovery but
    F5 -- and said nothing at all while the turn kept running server-side.

    A source assertion, which is unusual here and deliberate: the browser
    behaviour itself is not reachable from pytest, but "there is a close
    handler at all" is exactly the thing that was missing, and it is cheap to
    keep true.
    """
    source = (FRONTEND / "src" / "useThread.ts").read_text(encoding="utf-8")
    assert "socket.onclose" in source, "no close handler: a dropped socket is permanent"
    assert "onerror" in source
    assert "BACKOFF" in source, "reconnecting in a tight loop is its own fault"
    assert "resync" in source, "reconnecting without resyncing loses every event in the gap"


def test_the_frontend_skips_what_it_already_shows_and_buffers_while_it_syncs() -> None:
    """Source assertions, like F19-05's: pytest cannot drive a browser. What
    the browser does with these is measured by `probe_web.py browser`."""
    source = (FRONTEND / "src" / "useThread.ts").read_text(encoding="utf-8")
    assert "event.index < knownRef.current" in source, "replayed items are drawn again"
    assert "bufferRef.current.push(event)" in source, "events during a resync are lost"
    assert "record.approvals" in source, "a reload loses the pending approval card"
    assert "pending: true" in source, "the optimistic echo is never matched"
    # Subscribe first, on every page load -- not fetch-then-subscribe.
    assert "void resync(threadId)\n      .then(() => open())" not in source
```

> 最后一条断言检查的是"那段先拉后订阅的代码不在了"。源代码断言是最弱的一种测试——它只能说明那几行字在，说明不了它们对。它们存在的理由是：变异检查把其中任何一行删掉，至少会有一个测试变红（§14）。

**这一节真正的教训不是"写更多测试"。** 第一版有 40 个测试、32 条变异，全过——但它们全都在**单元**这一层：把事件喂给一个列表，检查列表。"后端发了什么"和"前端画了什么"各自都被测了，**它们接起来的样子**没有被测过。浏览器里点两下就能看到的问题，就落在这个接缝上。第 18 章改写时也说过同样的话：一个设计的两半各自都被测了，合起来的地方没有。

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
    """Every module in the package, subpackages included: (name, package, path).

    `rglob`, not `glob`.  The first version of this globbed one level, which
    was right for seventeen chapters because the package was flat -- and then
    chapter 19 added `web/`, eight modules that not one rule in this file could
    see.  Nothing failed.  The checker reported success on code it had never
    read, in the lint step, on every pull request.

    That is the worst shape a check can have, and it is worth naming: a rule
    that silently stops applying to new code is more dangerous than no rule,
    because the green tick keeps making the same promise after the promise has
    stopped being true.  Interlude B's whole argument was "state the rule where
    something executes it" -- this is the sequel, which is that the thing
    executing it has to be able to *see* the code.
    """
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

> - `pkg.rglob("*.py")`：递归找出所有 `.py` 文件。
> - `path.relative_to(pkg).with_suffix("").parts`：比如 `web/routes.py` → `("web", "routes")`。
> - 包的 `__init__.py` 就是这个包本身：`web/__init__.py` → `minicodex.web`。
> - 返回三样：模块名、它所在的包（解析 `from . import` 时要用）、文件路径。


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
def intra_package_imports(path: Path, package: str = PACKAGE) -> set[str]:
    """Every module inside the package that this file imports, at any depth.

    `ast.walk` rather than a scan of the top-level body, because an import
    written inside a function is still an edge -- and deferring an import is
    the standard trick for making a cycle stop failing at import time and
    start failing on the first call.  A checker that only reads the top of the
    file rewards exactly the repair that hides the problem.

    `package` is the module's own package, needed to resolve `from . import x`.
    Relative imports used to be skipped outright (`node.level == 0`), which was
    invisible while every module was flat and absolute -- and then chapter 19's
    subpackage was written the way subpackages are normally written, entirely
    in relative imports, and the checker saw an eight-module component with no
    edges at all.  Two holes, one symptom: the code was there and the rules
    were not looking at it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.split(".")[0] == PACKAGE)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from .foo import x` inside minicodex.web -> minicodex.web.foo
                # `from . import foo`                       -> minicodex.web.foo
                base = package.split(".")
                del base[len(base) - node.level + 1 :]
                anchor = ".".join(base)
                if node.module:
                    found.add(f"{anchor}.{node.module}")
                else:
                    found.update(f"{anchor}.{a.name}" for a in node.names)
                continue
            if not node.module or node.module.split(".")[0] != PACKAGE:
                continue
            # `from minicodex import compaction_prompt` is an edge to the
            # *package*, whose __init__ has to finish running first.  The first
            # version of this function dropped that case because the name is
            # not a module -- and reported "no cycles" for a package that could
            # not be imported at all.
            found.add(PACKAGE if node.module == PACKAGE else node.module)
    return found
```

> - `ast.parse` 把源代码解析成语法树（第 -1 章起的老朋友），`ast.walk` 走遍每一个节点。
> - `node.level`：`from .foo` 的点数。`from .foo import x` 是 1，`from ..foo` 是 2。
> - `base = package.split(".")`、`del base[len(base) - node.level + 1 :]`：在 `minicodex.web` 里，一个点就是 `minicodex.web` 本身。拼上 `node.module` 就得到被导入的模块全名。
> - `from . import foo`（没有 `module`）：被导入的是 `foo` 这个名字本身，`node.names` 里每一个都算一条边。


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
    """The price of breaking that cycle: two places name the same two models.

    Chapter 3's repair for exactly this shape -- when two places must agree and
    neither may import the other, the cheapest way to make them agree is a test
    that compares them.
    """
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


### 8.4 代码：检查器的其余改动

禁止边的表，最后一项是这一章加的；下面是入口模块的名单：

```python
FORBIDDEN: list[tuple[str, str, str]] = [
    ("tools", "agent", "FA-04: the loop is above the tool machinery, not below it"),
    # Chapter 10 met this cycle first; it is not on that chapter's list
    # (F10-06 there is runaway recursion), so it carries interlude B's ID.
    ("tools", "subagent", "FB-01: putting the spawn handler with the handlers"),
    (
        "subagent",
        "tools",
        "FB-01: the arrow that made the line above impossible. Interlude B "
        "inverted it -- subagent takes `build_tools` from its caller",
    ),
    ("history", "agent", "F01-08: the first cycle in this project"),
    ("agent_types", "*", "F01-08: the shared-type module only works while it is a leaf"),
    (
        "*",
        "web",
        "F19-08: the console is the outermost layer, beside __main__. Nothing "
        "below it may import it, or the agent grows a dependency on a browser",
    ),
]

# The modules that are entry points rather than layers: they may reach anywhere
# because nothing in the package reaches them.  `__main__` has always been one;
# chapter 19's `web` is the second, and adding it here rather than widening the
# rule is the point -- the exemption is a list you can read, not a wildcard.
TOP_LEVEL_ENTRY_POINTS = {"__main__"}
```

> 每一项是（谁，不许导入谁，为什么）。`"*"` 是"任何模块"。

建图、查禁止边（改用 `iter_modules`，并认识 `"*"`）：

```python
def build_graph(pkg: Path) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for name, package, path in iter_modules(pkg):
        graph[name] = intra_package_imports(path, package)
    known = set(graph)
    return {k: {v for v in vs if v in known and v != k} for k, vs in graph.items()}


def check_forbidden(pkg: Path) -> list[str]:
    problems = []
    for name, package, path in iter_modules(pkg):
        short = name[len(PACKAGE) + 1 :] or PACKAGE
        imports = intra_package_imports(path, package)
        for source, target, why in FORBIDDEN:
            if source == "*":
                # "nothing may import X", with two exemptions that are not
                # loopholes.  X's own modules import each other, which is
                # internal and not this rule's business.  And `__main__` is not
                # *below* the console, it is beside it: both are entry points,
                # and the CLI is how the console gets started (`minicodex
                # serve`).  The rule that matters is that nothing in the agent
                # -- agent, tools, composition, rollout -- can reach up here.
                if short in TOP_LEVEL_ENTRY_POINTS:
                    continue
                if short == target or short.startswith(f"{target}."):
                    continue
                hit = sorted(
                    i
                    for i in imports
                    if i == f"{PACKAGE}.{target}" or i.startswith(f"{PACKAGE}.{target}.")
                )
                if hit:
                    problems.append(f"{short} must not import {hit} ({why})")
                continue
            if source != short:
                continue
            if target == "*":
                ours = sorted(imports)
                if ours:
                    problems.append(
                        f"{short} must import nothing of ours, but imports {ours} ({why})"
                    )
            elif f"{PACKAGE}.{target}" in imports:
                problems.append(f"{short} must not import {target} ({why})")
    return problems
```

> - `short`：去掉 `minicodex.` 前缀的模块名，比如 `web.routes`。
> - `source == "*"` 那一支：两种豁免——被禁的那个包自己内部互相导入（`web.app` 导入 `web.routes` 当然可以）；入口模块（`__main__`）。其余任何模块导入 `minicodex.web` 或它下面的任何东西，都是一条违规。

剩下两处只是把 `glob` 换成 `iter_modules`：

```python
def check_one_agent_construction(pkg: Path) -> list[str]:
    problems = []
    for _name, _package, path in iter_modules(pkg):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Agent"
        )
        if calls and path.name not in AGENT_CONSTRUCTION_SITES:
            problems.append(
                f"{path.name} constructs an Agent. Build it through `Wiring.agent` "
                f"instead: a second construction site is how a child ended up "
                f"without a recorder, without compaction and without the "
                f"scheduler, silently, for a chapter (FB-01)."
            )
    return problems


def check_every_module_imports(pkg: Path) -> list[str]:
    """Import each module in a fresh interpreter, one subprocess per module.

    Not redundant with the cycle check, and the reason is measured: with
    `tools` and `subagent` importing each other, `python -c "import minicodex"`
    **exits 0**.  The package's `__init__` is a leaf, so the top-level import
    touches none of the broken part.  A smoke test that imports the package is
    the obvious CI check and it passes on a package that cannot be used.

    One subprocess per module, because once a module is in `sys.modules` the
    cycle is hidden from everything that runs afterwards -- which is also why
    the test suite stays green in a shell where the package has already been
    imported once.
    """
    failures: dict[str, list[str]] = {}
    for module, _package, _path in iter_modules(pkg):
        if module == PACKAGE:
            continue
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            env={**_env(), "PYTHONPATH": str(pkg.parent)},
        )
        if result.returncode != 0:
            last = (result.stderr.strip().splitlines() or ["(no output)"])[-1]
            failures.setdefault(last, []).append(module)

    # Grouped by message, because a cycle through `__init__` fails *every*
    # module with the same line: the first version of this printed twenty-five
    # identical paragraphs, which is a report nobody reads to the end.
    problems = []
    for message, modules in failures.items():
        if len(modules) > 3:
            problems.append(f"{len(modules)} modules do not import on their own: {message}")
        else:
            problems.extend(f"{m} does not import on its own: {message}" for m in modules)
    return problems
```

测试。加载检查器（它是 `scripts/` 下的一个脚本，不是包里的模块，所以要按文件路径加载）：

```python
def _load_checker() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "check_layers.py"
    spec = importlib.util.spec_from_file_location("check_layers", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_layers"] = module
    spec.loader.exec_module(module)
    return module


def test_F19_06_the_checker_walks_subpackages_not_just_the_top_level() -> None:
    """`pkg.glob("*.py")` was correct for eighteen chapters because the package
    was flat. Chapter 19 added `web/`, and every rule in `check_layers.py`
    stopped applying to eight new modules -- while still reporting success, in
    the lint step, on every pull request.
    """
    checker = _load_checker()
    names = {name for name, _package, _path in checker.iter_modules(SRC)}
    assert "minicodex.web" in names
    assert "minicodex.web.runtime" in names
    assert "minicodex.web.channel" in names
    assert "minicodex.agent" in names, "the flat modules are still covered"


def test_F19_06_the_import_graph_includes_the_subpackage() -> None:
    checker = _load_checker()
    graph = checker.build_graph(SRC)
    assert "minicodex.web.routes" in graph
    assert graph["minicodex.web.routes"], "a module with no edges is a module nothing read"


def test_F19_07_relative_imports_are_edges_too(tmp_path: Path) -> None:
    """`node.level == 0` skipped every `from .foo import x`.

    Fixing the glob alone was not enough and the second half was easy to miss:
    the subpackage became visible, and the checker still saw eight modules with
    no edges between them, because subpackages are written in relative imports.
    Two holes, one symptom -- the code was there and the rules were not looking.
    """
    checker = _load_checker()
    module = tmp_path / "thing.py"
    module.write_text(
        "from .approver import ApprovalBroker\nfrom . import events\nfrom ..agent import Wiring\n",
        encoding="utf-8",
    )
    found = checker.intra_package_imports(module, "minicodex.web")
    assert "minicodex.web.approver" in found
    assert "minicodex.web.events" in found
    assert "minicodex.agent" in found


def test_F19_07_the_console_modules_really_do_use_relative_imports() -> None:
    """Guards the test above from becoming vacuous: if the console were later
    rewritten in absolute imports, the relative-import path would stop being
    exercised by anything real and could rot unnoticed."""
    found = False
    for path in (SRC / "web").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = found or any(
            isinstance(node, ast.ImportFrom) and node.level for node in ast.walk(tree)
        )
    assert found


def test_F19_08_no_module_below_the_console_imports_it() -> None:
    """The console is an entry point, beside `__main__`, not a layer.

    It was written the other way round first: `web/store.py` reached up to
    `__main__.PROVIDERS` for two default model names, which is the cycle
    `__main__ -> web -> web.app -> web.routes -> web.store -> __main__`. It
    imported fine, because Python tolerates a cycle that resolves at call time,
    and `scripts/check_layers.py` only found it once it could see the
    subpackage at all (F19-06).
    """
    checker = _load_checker()
    assert checker.check_forbidden(SRC) == []
    assert checker.check_cycles(SRC) == []
```

> - `importlib.util.spec_from_file_location(...)` + `module_from_spec` + `exec_module`：从一个文件路径加载一个模块。
> - F19-07 的第一个测试在 `tmp_path` 里造一个假包，写一个用相对导入的文件，看检查器认不认得出来——不依赖真实的 `web/` 目录长什么样。
> - 第二个反过来：真的 `web/` 里确实用了相对导入。没有这一条，将来有人把 `web/` 全改成绝对导入，F19-07 的修复就再也没有东西在用它，而第一个测试还是绿的。

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
    """The three marks `agent.py` can write, each with something to say.

    A frontend assertion for the same reason as F19-05's: the drop was a
    missing branch, and a missing branch is exactly what a source check can
    still see.
    """
    source = (FRONTEND / "src" / "components" / "Chat.tsx").read_text(encoding="utf-8")
    agent = (SRC / "agent.py").read_text(encoding="utf-8")
    for mark in ("compacted", "budget_exhausted", "interrupted"):
        assert f'"{mark}"' in agent, f"agent.py no longer writes {mark}; update this test"
        assert mark in source, f"the console drops the {mark} mark"
```


两个方向都断言：控制台漏了一个会红，`agent.py` 改了名字也会红——
后者的报错信息直接告诉你去改测试，而不是让你困惑。


### 9.1 代码：`events.py`

```python
"""Turn what the agent already writes down into what the browser reads.

`History` calls an observer on every accepted item -- that hook is what makes
`RolloutWriter` work at all (chapter 7). Subclassing the writer rather than
adding a hook to the agent loop buys the console a property it could not get
any other way: **an event reaches the browser if and only if it was also
fsynced to disk.** The live view and the resumable transcript cannot disagree,
because there is one call site and it does both.

That is also why the console streams per history item rather than per token.
Token streaming is a second, parallel path out of the model client with its own
ordering and its own failure modes, and it has nothing to write to disk;
`probe_web.py stream` measures what the reader pays for that choice.

`mark` is forwarded too, and it is the reason this class is not just three
lines. The three marks the agent writes -- `compacted`, `budget_exhausted`,
`interrupted` -- are the only signal that the conversation the user is reading
is no longer the conversation the model is being sent. The first version of
this console emitted them and the frontend dropped every one, so a run could
compact away half its history with nothing on screen to say so.

Every `history_item` carries its `index`: where it sits in the history this
file will load back as. That number arrived when chapter 19 was rewritten, and
it exists because the writer is told about items the browser already has. A
resumed turn starts by replaying the whole conversation into the new file
(`agent._attach`), and a compaction writes its new baseline the same way --
both through `append`, both indistinguishable here from a new item. The first
version streamed all of it, so every follow-up message drew the previous turns
on screen a second time. The index is what lets the browser tell "the 7th item
of this conversation, which I already show" from "the 8th, which is new",
without the agent having to learn that a browser exists.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from minicodex.rollout import RolloutWriter, SessionMeta, _dump_item

EventSink = Callable[[dict[str, Any]], None]


class StreamingRolloutWriter(RolloutWriter):
    """A `RolloutWriter` that also says, out loud, what it just wrote."""

    def __init__(self, path: Path, meta: SessionMeta, emit: EventSink) -> None:
        super().__init__(path, meta)
        self._emit = emit
        #: Called after each item is durable, to let the run report state that
        #: is not itself a history item -- the plan, and the sandbox mode.
        #: Assigned by `runtime.run_turn`; see `_derived` there.
        self.after_item: Callable[[], None] | None = None
        #: How many items the history this file loads back as holds so far.
        #: `read_rollout` keeps what follows the last `compacting` marker, so
        #: that marker is where counting starts again.
        self._position = 0

    def append(self, item: Any) -> None:  # type: ignore[override]
        super().append(item)
        index, self._position = self._position, self._position + 1
        self._emit({"type": "history_item", "index": index, "item": _dump_item(item)})
        if self.after_item is not None:
            self.after_item()

    def mark(self, kind: str, **payload: Any) -> None:  # type: ignore[override]
        super().mark(kind, **payload)
        if kind == "compacting":
            self._position = 0
        self._emit({"type": "mark", "mark": kind, "payload": payload, "position": self._position})
```

> - 说明的最后一段是这次改写加的（§7.11）。
> - `from minicodex.rollout import ... _dump_item`：第 7 章把一条历史记录变成字典的函数。名字带下划线，按 Python 的习惯是"模块内部用的"；这里借用它，是因为浏览器要的正是文件里写的那个形状。
> - `EventSink = Callable[[dict[str, Any]], None]`：给"收一个字典、不返回东西的函数"起个名字。
> - **`_position`**：读回来时这段历史有多长。`append` 用当前值做这一条的 `index`，然后加一；`mark("compacting")` 把它清零（§7.11）。

```python
async def test_F19_09_marks_are_emitted_as_their_own_event(tmp_path: Path) -> None:
    """`compacted`, `budget_exhausted` and `interrupted` are the only signal
    that the conversation on screen is no longer the conversation being sent.
    The backend emitted all three and the frontend dropped the whole event
    type, so a run could compact half its history away in silence.
    """
    from minicodex.rollout import SessionMeta, new_session_id
    from minicodex.web.events import StreamingRolloutWriter

    events: list[dict[str, Any]] = []
    meta = SessionMeta(
        session_id=new_session_id(),
        created=0.0,
        cwd=str(tmp_path),
        provider="ollama",
        model="m",
        sandbox_mode="read-only",
        approval_policy="on-request",
    )
    writer = StreamingRolloutWriter(tmp_path / "s.jsonl", meta, events.append)
    try:
        writer.mark("compacted", generation=1, replaced=12)
    finally:
        writer.release()

    assert events == [
        {
            "type": "mark",
            "mark": "compacted",
            "payload": {"generation": 1, "replaced": 12},
            "position": 0,
        }
    ]
```

> 断言的是**整个**事件，包括 `position`——这一条在改写时跟着变了。

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
    """An absolute path to a directory that exists.

    `run_turn` does `root.mkdir(parents=True, exist_ok=True)` because the agent
    has to be able to work in a directory that is being created around it. That
    is the wrong behaviour for *this* string, which is a person typing a path
    into a browser: a typo used to silently create a directory tree anywhere
    the server user could write, and then run an agent inside it.
    """
    text = raw.strip()
    if not text:
        raise HTTPException(400, "workspace path cannot be empty")
    path = Path(text).expanduser()
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


（`validate_settings` 的全文在 §2.5。）


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


### 10.3 改写时发现：`"false"` 会把记忆打开

第一版 `validate_settings` 的最后两行：

```python
    for flag in ("memory", "remember", "skills"):
        merged[flag] = bool(merged[flag])
```

本意是"把各种写法统一成 `True`/`False`"。可是 Python 里 `bool("false")` 是 `True`——任何非空字符串都是真。一个手写的请求如果把开关写成字符串：

```
$ uv run python -c "from minicodex.web.store import validate_settings; print(validate_settings({'memory': 'false'})['memory'])"
True        # 第一版
```

**一个明确说"不要"的请求，把记忆打开了。** §10.2 刚说完"默认值不是一次同意"，这里是更糟的版本：一次拒绝被当成了同意。浏览器自己发的是真正的布尔值，碰不到这个问题；但会撞上它的，正是那些不通过界面、直接调接口的人——脚本、别的工具。

修法：不猜，拒绝。不是布尔值就报错（§2.5 那段代码的最后一个 `for`），路由把它变成 400。

```python
def test_a_switch_spelled_as_a_string_is_refused_not_switched_on() -> None:
    """`bool("false")` is `True`: the first version turned memory on for a
    request that asked for it to be off."""
    for flag in ("memory", "remember", "skills"):
        with pytest.raises(ValueError):
            validate_settings({flag: "false"})
    assert validate_settings({"memory": True})["memory"] is True
```

### 10.4 一个这一章没修的错：记忆面板读错了目录

逐行读 `routes.py` 时还看到一处，这一次**没有修**，原因在下面。

记忆相关的三个路由——看记忆、看写入队列、全部忘掉——都在**工作区**下面找：

```python
@router.get("/api/threads/{thread_id}/memory")
def get_memory(request: Request, thread_id: str) -> dict[str, Any]:
    console = _console(request)
    record = console.require_thread(thread_id)
    directory = Path(record["workspace"]) / ".minicodex" / "memories"
    try:
        memory = load_memory(directory)
    except Exception as exc:
        return {"directory": str(directory), "error": str(exc), "entries": []}
    counts = usage(directory)
    return {
        "directory": str(directory),
        "summary": memory.summary,
        "describe": memory.describe(),
        "empty_reason": memory.empty_reason,
        "files": {"body": BODY_FILE, "summary": SUMMARY_FILE},
        "entries": [
            {
                "id": entry.entry_id,
                "title": entry.title,
                "headline": entry.headline(),
                "lines": entry.lines,
                "usage": counts.get(entry.entry_id),
            }
            for entry in memory.entries
        ],
    }


@router.get("/api/threads/{thread_id}/memory/jobs")
def get_memory_jobs(request: Request, thread_id: str) -> dict[str, Any]:
    console = _console(request)
    record = console.require_thread(thread_id)
    path = Path(record["workspace"]) / ".minicodex" / "memory_jobs.sqlite3"
    if not path.exists():
        return {"counts": {}, "rows": []}
    with JobStore(path) as jobs:
        return {"counts": jobs.counts(), "rows": jobs.rows()}


@router.post("/api/threads/{thread_id}/memory/forget-all")
def forget_all_memory(request: Request, thread_id: str) -> dict[str, Any]:
    """The one-click reset, and it is not optional.

    codex ships `Reset all memories?` next to `Enable memories?` in the same
    dialog, and the reason is the same reason this console asks twice before
    switching writing on: a feature that accumulates what it learned about you
    has to come with a way to end that, in the place you turned it on.
    """
    console = _console(request)
    record = console.require_thread(thread_id)
    directory = Path(record["workspace"]) / ".minicodex" / "memories"
    removed = []
    for name in (BODY_FILE, SUMMARY_FILE, "usage.json"):
        target = directory / name
        if target.exists():
            target.unlink()
            removed.append(name)
    jobs_path = Path(record["workspace"]) / ".minicodex" / "memory_jobs.sqlite3"
    if jobs_path.exists():
        with JobStore(jobs_path) as jobs:
            jobs.forget_all()
    return {"removed": removed}
```

而每一轮对话读的是 `runtime.load_feature_dirs` 里的 `DEFAULT_MEMORY_DIR`——第 16 章改写时把记忆搬到了 `~/.minicodex/memories`（§2.6）。两边说的不是同一个目录：

- 记忆面板显示的是一个**没有任何一轮对话读过**的目录——通常是空的，哪怕 `~/.minicodex/memories` 里有满满一个文件。
- "全部忘掉"删的是那个空目录，Agent 真正在用的记忆一个字都没动；它还会回答"删掉了 `MEMORY.md`"——如果工作区下面恰好有一个的话。
- 设置面板上写着"注入 `.minicodex/memories`"，同样是旧路径。
- 测试 `test_F19_11_everything_it_learned_can_be_erased_from_where_it_was_enabled` 是绿的，因为它也在工作区下面造文件——测试和代码犯的是同一个错。

这是一个**同意**层面的问题：§10.2 说"一个会积累你信息的功能，必须在你打开它的地方提供结束它的办法"——这个办法在这里是坏的，而且不出声。

**为什么不在这里修？** 第 23 章（多用户）要把记忆改成"每个用户一份"，改到这几个路由时发现了它，编号 F23-05，专门写了一节"四章之前就错的那个目录，和一个一直绿着的测试"，还留了一个练习让读者把它装回去看看。这本书记录的是真实的构建过程：这个错误在第 19 章被写进去，到第 23 章才被发现。在这里悄悄修掉，第 23 章那一节就成了在讲一个不存在的 bug。所以这里只把它指出来——如果你照着第 19–22 章的代码用控制台，**不要相信记忆面板和那个"全部忘掉"按钮**，记忆在 `~/.minicodex/memories`，要删就直接删那个目录。

（第 17 章 F17-12 把写入队列也搬到了 `~/.minicodex/memory_jobs.sqlite3`，`get_memory_jobs` 读的同样是旧位置。）

### 10.5 代码：`routes.py` 其余的路由

到这里，`routes.py` 里讲过的有：开头（§3.2）、发消息和停止（§3.2）、WebSocket（§6.1）、回答审批（§4.2）、路径校验（§10.1）、记忆（§10.4）。剩下的都是"增删改查"的形状，一次贴完。

策略和工具——前端的选项从这里来，所以界面上不可能出现核心没实现的值：

```python
@router.get("/api/policy")
def get_policy() -> dict[str, Any]:
    return {
        "sandbox_modes": list(SANDBOX_MODES),
        "approval_policies": list(APPROVAL_POLICIES),
        "defaults": dict(DEFAULT_THREAD_SETTINGS),
        "tables": policy_tables(),
    }


@router.get("/api/tools")
def get_tools() -> list[dict[str, str]]:
    """Informational: minicodex has no per-tool switch, so neither does this."""
    return [
        {
            "name": t["function"]["name"],
            "description": (t["function"].get("description") or "").split("\n")[0],
        }
        for t in TOOL_SCHEMAS
    ]
```

工作区和会话：

```python
class NewThread(BaseModel):
    workspace: str
    title: str = "New session"
    settings: dict[str, Any] = {}


class ThreadPatch(BaseModel):
    title: str | None = None
    settings: dict[str, Any] | None = None


@router.get("/api/workspaces")
def get_workspaces(request: Request) -> list[dict[str, Any]]:
    console = _console(request)
    groups = console.store.workspaces()
    for group in groups:
        for thread in group["threads"]:
            sessions = list_sessions(console.store.thread_dir(thread["id"]))
            thread["message_count"] = len(sessions[0].items) if sessions else 0
            thread["model"] = sessions[0].meta.model if sessions else None
            thread["busy"] = thread["id"] in console.busy
    return groups


@router.post("/api/threads")
def create_thread(request: Request, body: NewThread) -> dict[str, Any]:
    console = _console(request)
    workspace = _validated_workspace(body.workspace)
    try:
        settings = validate_settings(body.settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return console.store.new_thread(workspace, body.title.strip() or "New session", settings)


@router.get("/api/threads/{thread_id}")
def get_thread(request: Request, thread_id: str) -> dict[str, Any]:
    console = _console(request)
    record = console.require_thread(thread_id)
    directory = console.store.thread_dir(thread_id)
    items: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    try:
        loaded = read_rollout(resolve("last", directory))
    except RolloutError:
        pass  # no turns yet
    else:
        history, _dropped = loaded.history()
        items = [_dump_item(item) for item in history.items]
        marks = list(getattr(loaded, "marks", []) or [])
    return {
        **record,
        "items": items,
        "marks": marks,
        "busy": thread_id in console.busy,
        "turns": len(list_sessions(directory)),
        # Asked while nobody was watching, or before a reload: the event is
        # gone, the turn is still waiting. See approver.py.
        "approvals": console.broker.asked(str(directory)),
    }


@router.patch("/api/threads/{thread_id}")
def patch_thread(request: Request, thread_id: str, body: ThreadPatch) -> dict[str, Any]:
    console = _console(request)
    record = console.require_thread(thread_id)
    changes: dict[str, Any] = {}
    if body.title is not None and body.title.strip():
        changes["title"] = body.title.strip()
    if body.settings is not None:
        if thread_id in console.busy:
            raise HTTPException(409, "cannot change settings while this thread is answering")
        try:
            changes["settings"] = validate_settings({**record["settings"], **body.settings})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if not changes:
        return record
    updated = console.store.update("threads.json", thread_id, **changes)
    assert updated is not None
    return updated


@router.delete("/api/threads/{thread_id}")
def delete_thread(request: Request, thread_id: str) -> dict[str, bool]:
    console = _console(request)
    if thread_id in console.busy:
        raise HTTPException(409, "cannot delete a thread while it is answering")
    return {"ok": console.store.delete("threads.json", thread_id)}


@router.get("/api/threads/{thread_id}/sessions")
def thread_sessions(request: Request, thread_id: str) -> list[dict[str, Any]]:
    """Every rollout file this thread has written, newest first.

    A thread is a directory of sessions, not one file: every turn writes a new
    one and resumes from the last (chapter 7). This is what the fork menu picks
    from.
    """
    console = _console(request)
    console.require_thread(thread_id)
    return [
        {
            "session_id": s.meta.session_id,
            "created": s.meta.created,
            "model": s.meta.model,
            "messages": len(s.items),
            "forked_from": s.meta.forked_from,
        }
        for s in list_sessions(console.store.thread_dir(thread_id))
    ]
```

> - `get_workspaces`：每个会话顺便算出"有几条记录、用的哪个模型、忙不忙"，侧边栏要显示。
> - `get_thread`：读这个会话目录里最后一个 rollout 文件，把历史变成字典列表——这就是浏览器重新同步时拿到的快照。`"approvals"` 那一行是改写时加的（§4.3）。
> - `patch_thread`：改标题随时可以；改设置在一轮进行中不行（409），而且要过 `validate_settings`。

分叉（从某一条记录开始复制出一个新会话）：

```python
class ForkBody(BaseModel):
    session: str = "last"
    upto: int | None = None
    title: str | None = None


@router.post("/api/threads/{thread_id}/fork")
def fork_thread(request: Request, thread_id: str, body: ForkBody) -> dict[str, Any]:
    """Copy a prefix of this thread's history into a brand new thread.

    `rollout.fork` is chapter 7's, unchanged; all this adds is that the copy
    lands in a *different* thread directory, so the original is still there to
    go back to.
    """
    console = _console(request)
    record = console.require_thread(thread_id)
    source_dir = console.store.thread_dir(thread_id)
    fresh = console.store.new_thread(
        record["workspace"],
        body.title or f"{record['title']} (fork)",
        record["settings"],
    )
    try:
        path = fork(body.session, upto=body.upto, directory=source_dir)
    except RolloutError as exc:
        console.store.delete("threads.json", fresh["id"])
        raise HTTPException(400, str(exc)) from exc
    target = console.store.thread_dir(fresh["id"]) / path.name
    target.write_bytes(path.read_bytes())
    path.unlink()
    return fresh
```

> `rollout.fork` 是第 7 章的，一行没改；这里只是把它产出的文件挪到**新**会话的目录里，原来的会话还在。

规则（第 5 章"总是允许"存下来的东西，可以在这里撤销）：

```python
def _rules(console: Any, thread_id: str) -> RuleStore:
    record = console.require_thread(thread_id)
    return RuleStore(Path(record["workspace"]) / ".minicodex" / "rules.json")


@router.get("/api/threads/{thread_id}/rules")
def get_rules(request: Request, thread_id: str) -> list[dict[str, Any]]:
    """What "always allow" has actually agreed to, so it can be taken back.

    Only project-scoped rules survive a process, so this lists what is on disk
    for the workspace. A rule that can only be revoked by finding and editing a
    JSON file is a rule that stays -- chapter 5 says so about the CLI's
    `minicodex forget N`, and it is no less true with a browser in front of it.
    """
    store = _rules(_console(request), thread_id)
    return [
        {
            "index": i,
            "words": list(rule.words),
            "scope": rule.scope,
            "prompted_by": rule.prompted_by,
            "created_at": rule.created_at,
            "describe": rule.describe(),
        }
        for i, rule in enumerate(store.all())
    ]


@router.delete("/api/threads/{thread_id}/rules/{index}")
def revoke_rule(request: Request, thread_id: str, index: int) -> dict[str, str]:
    store = _rules(_console(request), thread_id)
    try:
        rule = store.forget(index)
    except IndexError as exc:
        raise HTTPException(404, f"no rule {index}") from exc
    return {"revoked": " ".join(rule.words)}
```

技能和插件：

```python
@router.get("/api/threads/{thread_id}/skills")
def get_skills(request: Request, thread_id: str) -> dict[str, Any]:
    """Real, as of chapter 18. This tab used to say the subsystem did not exist."""
    console = _console(request)
    record = console.require_thread(thread_id)
    directory = Path(record["workspace"]) / ".minicodex" / "skills"
    found = discover_skills(directory)
    return {
        "supported": True,
        "directory": str(directory),
        "describe": found.describe(),
        "skipped": list(found.skipped),
        "items": [
            {
                "name": skill.name,
                "description": skill.description,
                "body": skill.body,
                "path": str(skill.path),
                "catalog_line": skill.catalog_line(),
            }
            for skill in found.skills
        ],
    }


@router.get("/api/plugins")
def get_plugins() -> dict[str, Any]:
    """Still not real, and saying so is the point.

    codex has a plugin/bundle mechanism; minicodex has no module for one. An
    empty list would let the tab imply it had looked and found nothing.
    """
    return {"supported": False, "items": []}
```

提供方。`_public` 保证 key 永远不会发回浏览器——连输入它的那个浏览器也不发：

```python
class ProviderIn(BaseModel):
    name: str
    provider: str = "openai"
    base_url: str
    model: str
    api_key: str | None = None


def _public(provider: dict[str, Any]) -> dict[str, Any]:
    """A key never leaves the process, not even to the browser that typed it."""
    return {**provider, "api_key": bool(provider.get("api_key"))}


@router.get("/api/providers")
def list_providers(request: Request) -> list[dict[str, Any]]:
    return [_public(p) for p in _console(request).store.providers()]


@router.post("/api/providers")
def add_provider(request: Request, body: ProviderIn) -> dict[str, Any]:
    console = _console(request)
    if body.provider not in ("openai", "ollama"):
        raise HTTPException(400, f"unknown provider kind {body.provider!r}")
    record = console.store.add("providers.json", {**body.model_dump(), "active": False})
    return _public(record)


@router.post("/api/providers/{provider_id}/activate")
def activate_provider(request: Request, provider_id: str) -> dict[str, bool]:
    if not _console(request).store.activate_provider(provider_id):
        raise HTTPException(404, "no such provider")
    return {"ok": True}


@router.delete("/api/providers/{provider_id}")
def delete_provider(request: Request, provider_id: str) -> dict[str, bool]:
    return {"ok": _console(request).store.delete("providers.json", provider_id)}
```

MCP 服务器：

```python
class McpIn(BaseModel):
    name: str
    command: str  # shell-style; split with shlex, see below
    env: dict[str, str] = {}
    cwd: str | None = None
    startup_timeout: float = 30.0
    tool_timeout: float = 60.0


@router.get("/api/mcp")
def list_mcp(request: Request) -> list[dict[str, Any]]:
    return _console(request).store.all("mcp.json")


@router.post("/api/mcp")
def add_mcp(request: Request, body: McpIn) -> dict[str, Any]:
    command = shlex.split(body.command)
    if not command:
        raise HTTPException(400, "command cannot be empty")
    if body.startup_timeout <= 0 or body.tool_timeout <= 0:
        raise HTTPException(400, "timeouts must be positive")
    return _console(request).store.add(
        "mcp.json",
        {
            "name": body.name,
            "command": command,
            "env": body.env,
            "cwd": body.cwd,
            "startup_timeout": body.startup_timeout,
            "tool_timeout": body.tool_timeout,
            "enabled": True,
        },
    )


class McpToggle(BaseModel):
    enabled: bool


@router.post("/api/mcp/{server_id}/toggle")
def toggle_mcp(request: Request, server_id: str, body: McpToggle) -> dict[str, Any]:
    record = _console(request).store.update("mcp.json", server_id, enabled=body.enabled)
    if record is None:
        raise HTTPException(404, "no such server")
    return record


@router.delete("/api/mcp/{server_id}")
def delete_mcp(request: Request, server_id: str) -> dict[str, bool]:
    return {"ok": _console(request).store.delete("mcp.json", server_id)}
```

> `shlex.split(body.command)`：表单里填的是一整行命令，像 shell 一样切成参数列表，第 9 章的 `ServerConfig` 要的是列表。

这些路由的测试：

```python
def test_an_api_key_is_never_sent_back_to_the_browser(
    console: TestClient,
) -> None:
    """It has to be stored -- there is nowhere else for a typed key to live --
    but a GET that returns it puts it in every browser cache and devtools log
    that ever renders the settings page."""
    console.post(
        "/api/providers",
        json={
            "name": "secret",
            "provider": "openai",
            "base_url": "https://example.test/v1",
            "model": "m",
            "api_key": "sk-do-not-echo-this",
        },
    )
    body = console.get("/api/providers").text
    assert "sk-do-not-echo-this" not in body
    assert '"api_key":true' in body.replace(" ", "")


def test_the_console_reads_every_mcp_field_the_form_can_set(
    console: TestClient,
) -> None:
    """`env`, `cwd` and the two timeouts were read by `run_turn` from the first
    version and settable by nobody, so every server ran on the defaults for
    ever."""
    created = console.post(
        "/api/mcp",
        json={
            "name": "notes",
            "command": "python -m server --flag",
            "env": {"MCP_NOTES_DB": "/tmp/notes.json"},
            "cwd": "/tmp",
            "startup_timeout": 5.0,
            "tool_timeout": 7.0,
        },
    ).json()
    assert created["command"] == ["python", "-m", "server", "--flag"]
    assert created["env"] == {"MCP_NOTES_DB": "/tmp/notes.json"}
    assert created["cwd"] == "/tmp"
    assert created["startup_timeout"] == 5.0
    assert created["tool_timeout"] == 7.0


def test_the_approval_card_can_reach_both_remember_scopes(console: TestClient) -> None:
    """The backend accepted `project` from the first version and the card only
    ever offered `session`, so the durable half of chapter 5's rule store was
    unreachable from a browser."""
    source = (FRONTEND / "src" / "components" / "Chat.tsx").read_text(encoding="utf-8")
    assert '"project"' in source and '"session"' in source

    rejected = console.post(
        "/api/approvals/nope/respond",
        json={"approved": True, "command": "ls", "remember": "forever"},
    )
    assert rejected.status_code == 400


def test_the_plugins_tab_says_it_is_not_real(console: TestClient) -> None:
    """An empty list would imply the tab had looked and found nothing. There is
    no plugin module in this package to look with."""
    assert console.get("/api/plugins").json() == {"supported": False, "items": []}


def test_the_skills_tab_is_real_now(console: TestClient, workspace: Path) -> None:
    """It used to say the subsystem did not exist. `skills.py` has existed
    since chapter 18, and copy that is wrong about its own repository is worse
    than a missing tab."""
    skills = workspace / ".minicodex" / "skills" / "run-tests"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: run-tests\ndescription: how this project runs its tests\n---\n\n"
        "uv run pytest\n",
        encoding="utf-8",
    )
    thread_id = _thread(console, workspace, skills=True)

    body = console.get(f"/api/threads/{thread_id}/skills").json()
    assert body["supported"] is True
    assert [s["name"] for s in body["items"]] == ["run-tests"]
    assert "uv run pytest" in body["items"][0]["body"]
```

> 第二个是第 3 章那条规矩的又一次：表单能填的每一个字段（环境变量、工作目录、两个超时），`run_turn` 都得真的读——否则是一个看起来能用、其实被忽略的设置。

### 10.6 代码：会话设置面板

```tsx
import { useState } from "react";
import type { PolicyInfo } from "../api";
import type { ThreadSettings } from "../types";

interface Props {
  policy: PolicyInfo;
  value: ThreadSettings;
  onChange: (next: ThreadSettings) => void;
  disabled?: boolean;
}

/**
 * The whole 3x3 permission matrix, plus the three feature switches.
 *
 * The previous console hardcoded `workspace-write` / `on-request` in the
 * backend and showed neither in the UI, so the one thing a person most needs
 * to know before letting an agent loose in a directory -- what it may do
 * without asking -- was not on the screen at all. The options come from
 * `GET /api/policy`, which reads `policy.SANDBOX_MODES` and
 * `policy.APPROVAL_POLICIES`, so this list cannot drift from what the core
 * actually implements.
 */
export function ThreadSettingsForm({ policy, value, onChange, disabled }: Props) {
  // Memory *writing* asks twice. Not a nag: chapter 17's F17-11 is that
  // switching it on changes what every later turn does, in the background,
  // using the same quota, on transcripts the person has not re-read. codex's
  // TUI puts `Enable memories?` behind a confirmation for the same reason.
  const [confirmingRemember, setConfirmingRemember] = useState(false);

  const set = <K extends keyof ThreadSettings>(key: K, next: ThreadSettings[K]) =>
    onChange({ ...value, [key]: next });

  const modeHelp: Record<string, string> = {
    "read-only": "It may read and run commands that only read. Anything that writes gets asked.",
    "workspace-write": "It may write inside this workspace without asking. Outside it, asked.",
    "full-access": "No sandbox. Everything the server user can do, it can do.",
  };
  const policyHelp: Record<string, string> = {
    never: "Never ask. Anything the sandbox does not allow is refused outright.",
    "on-request": "Ask when the sandbox is not enough, and let the model request more.",
    "unless-trusted": "Ask for everything except commands already known to be safe.",
  };

  return (
    <div className="settings-form">
      <div className="settings-group">
        <label className="settings-label">Sandbox mode</label>
        <div className="radio-row">
          {policy.sandbox_modes.map((mode) => (
            <button
              key={mode}
              type="button"
              disabled={disabled}
              className={`radio-pill${value.sandbox_mode === mode ? " on" : ""}`}
              onClick={() => set("sandbox_mode", mode)}
            >
              {mode}
            </button>
          ))}
        </div>
        <p className="settings-help">{modeHelp[value.sandbox_mode]}</p>
      </div>

      <div className="settings-group">
        <label className="settings-label">Approval policy</label>
        <div className="radio-row">
          {policy.approval_policies.map((item) => (
            <button
              key={item}
              type="button"
              disabled={disabled}
              className={`radio-pill${value.approval_policy === item ? " on" : ""}`}
              onClick={() => set("approval_policy", item)}
            >
              {item}
            </button>
          ))}
        </div>
        <p className="settings-help">{policyHelp[value.approval_policy]}</p>
      </div>

      <div className="settings-group">
        <label className="settings-label" htmlFor="ctx">
          Context window (tokens)
        </label>
        <input
          id="ctx"
          className="settings-input"
          inputMode="numeric"
          disabled={disabled}
          placeholder="leave empty to never compact"
          value={value.context_window ?? ""}
          onChange={(e) => {
            const raw = e.target.value.trim();
            set("context_window", raw === "" ? null : Number(raw));
          }}
        />
        <p className="settings-help">
          Compaction is <strong>off</strong> without this. A long thread then grows until the
          provider refuses it rather than summarising itself (chapter 6).
        </p>
      </div>

      <div className="settings-group">
        <label className="settings-label">Features</label>

        <label className="check-row">
          <input
            type="checkbox"
            disabled={disabled}
            checked={value.skills}
            onChange={(e) => set("skills", e.target.checked)}
          />
          <span>
            <strong>Skills</strong> — put the catalog of{" "}
            <code>.minicodex/skills/*/SKILL.md</code> in every request, and let the model open one
            when it wants the body (chapter 18).
          </span>
        </label>

        <label className="check-row">
          <input
            type="checkbox"
            disabled={disabled}
            checked={value.memory}
            onChange={(e) => set("memory", e.target.checked)}
          />
          <span>
            <strong>Read memory</strong> — inject <code>.minicodex/memories</code> into every
            request (chapter 16).
          </span>
        </label>

        <label className="check-row">
          <input
            type="checkbox"
            disabled={disabled}
            checked={value.remember}
            onChange={(e) => {
              if (e.target.checked) setConfirmingRemember(true);
              else set("remember", false);
            }}
          />
          <span>
            <strong>Write memory</strong> — after each turn, a background job reads this thread's
            transcript and updates the memory files (chapter 17).
          </span>
        </label>

        {confirmingRemember && (
          <div className="confirm-box">
            <div className="confirm-title">Let it write down what it learns?</div>
            <p>
              A second model call runs beside every turn, reads the whole transcript — including
              the contents of every file the agent opened — and edits{" "}
              <code>.minicodex/memories</code>. It costs tokens you did not ask for, and it
              changes what every later turn sends.
            </p>
            <p>
              Secrets are scrubbed before anything is written, and you can erase all of it from{" "}
              <strong>Extensions → Memory</strong> at any time.
            </p>
            <div className="ac-actions">
              <button
                className="btn btn-primary btn-sm"
                onClick={() => {
                  set("remember", true);
                  setConfirmingRemember(false);
                }}
              >
                Turn it on
              </button>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => setConfirmingRemember(false)}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
```

> - 沙箱模式和审批策略的选项，来自 `GET /api/policy`（`policy.sandbox_modes`），不是写死在前端。
> - `confirmingRemember`：勾"写记忆"不会直接生效，先弹出一段说明（会读整段对话、会花 token、会改变以后每一轮发出去的内容、可以在哪里清掉），点"Turn it on"才真的打开。
> - 上下文窗口输入框：空的就是 `null`（不压缩）。

§10 的测试：

```python
def test_F19_10_a_relative_workspace_is_refused(console: TestClient) -> None:
    response = console.post("/api/threads", json={"workspace": "src/minicodex"})
    assert response.status_code == 400
    assert "absolute" in response.json()["detail"]


def test_F19_10_a_workspace_that_does_not_exist_is_refused(
    console: TestClient, tmp_path: Path
) -> None:
    """`run_turn` used to `mkdir(parents=True)` whatever it was handed, so one
    typo created a directory tree anywhere the server user could write and then
    started an agent inside it."""
    target = tmp_path / "typpo" / "project"
    response = console.post("/api/threads", json={"workspace": str(target)})
    assert response.status_code == 400
    assert not target.exists(), "a rejected path must not be created on the way to being rejected"
    # The *message*, not just the status. Deleting the `exists()` check leaves
    # the `is_dir()` one below it, which also answers 400 for a path that is
    # not there -- so a test that only reads the status cannot tell the two
    # apart, and mutation testing reported the check as dead when it is not.
    # Asserting the sentence makes the two branches distinguishable.
    assert "no such directory" in response.json()["detail"]


def test_F19_10_a_file_is_not_a_workspace(console: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    response = console.post("/api/threads", json={"workspace": str(target)})
    assert response.status_code == 400
    assert "not a directory" in response.json()["detail"]


def test_F19_10_an_invented_sandbox_mode_is_refused(console: TestClient, workspace: Path) -> None:
    """`Session.mode` is a `Literal` nothing checks at runtime, and an unknown
    mode does not fail -- it falls through every branch in `judge_command` and
    lands on whichever default that branch has. Not "denied": unpredictable.
    """
    response = console.post(
        "/api/threads",
        json={"workspace": str(workspace), "settings": {"sandbox_mode": "yolo"}},
    )
    assert response.status_code == 400

    with pytest.raises(ValueError):
        validate_settings({"approval_policy": "sometimes"})


def test_F19_10_the_ui_offers_exactly_what_the_core_implements(console: TestClient) -> None:
    """The list in the browser comes from `policy.py`, so a mode can never be
    offered that the core does not have, nor a new one hidden."""
    body = console.get("/api/policy").json()
    assert body["sandbox_modes"] == list(SANDBOX_MODES)
    assert body["approval_policies"] == list(APPROVAL_POLICIES)


def test_F19_11_memory_and_skills_are_off_until_asked_for() -> None:
    """F17-11, in a browser.

    Switching memory on changes what every later turn sends; switching writing
    on starts a second model call beside every turn, on the same quota, reading
    transcripts nobody has re-read. A switch that defaults to on has not been
    consented to -- it has been assumed.
    """
    assert DEFAULT_THREAD_SETTINGS["memory"] is False
    assert DEFAULT_THREAD_SETTINGS["remember"] is False
    assert DEFAULT_THREAD_SETTINGS["skills"] is False
    assert DEFAULT_THREAD_SETTINGS["sandbox_mode"] == "read-only"


def test_F19_11_reading_and_writing_memory_are_separate_switches(
    console: TestClient, workspace: Path
) -> None:
    """`--remember` without `--memory` is a real configuration and the console
    has to be able to express it, or the two consents have been merged into
    one behind the user's back."""
    thread_id = _thread(console, workspace, remember=True)
    settings = console.get(f"/api/threads/{thread_id}").json()["settings"]
    assert settings["remember"] is True
    assert settings["memory"] is False


def test_F19_11_writing_memory_is_confirmed_a_second_time() -> None:
    """codex's TUI puts `Enable memories?` behind a confirmation, with `Reset
    all memories?` next to it. Both, here, for the same reason."""
    source = (FRONTEND / "src" / "components" / "ThreadSettings.tsx").read_text(encoding="utf-8")
    assert "confirmingRemember" in source, "writing memory is switched on with one click"


def test_F19_11_everything_it_learned_can_be_erased_from_where_it_was_enabled(
    console: TestClient, workspace: Path
) -> None:
    memories = workspace / ".minicodex" / "memories"
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("v1\n\n## thing\nbody\n", encoding="utf-8")
    thread_id = _thread(console, workspace, memory=True)

    response = console.post(f"/api/threads/{thread_id}/memory/forget-all")
    assert response.status_code == 200
    assert "MEMORY.md" in response.json()["removed"]
    assert not (memories / "MEMORY.md").exists()
```

> 最后一个就是 §10.4 说的那个"一直绿着的测试"。

还有两个续接的测试，是第 16、18 章改写时加进这个文件的（那两章各自讲了原因）：

```python
async def test_a_follow_up_message_does_not_repeat_agents_md_or_memory(
    tmp_path: Path, stub_url: str, served_requests: list[dict], monkeypatch: Any
) -> None:
    """Every message in a browser thread is a resume: the thread is reloaded
    from its rollout file and a new agent is built. Both watchers were built
    without being told what the conversation already held, so each follow-up
    message added another copy of AGENTS.md and of the memory block --
    chapter 13's resume fix and chapter 16's had reached `__main__` and not
    this file. Found when chapter 16 was rewritten.
    """
    from minicodex.memory import BODY_FILE, DATA_PREAMBLE, SUMMARY_FILE
    from minicodex.web import runtime

    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    (memory_dir / SUMMARY_FILE).write_text("v1\n\n- Run tests as `python -m pytest`.\n")
    (memory_dir / BODY_FILE).write_text("v1\n\n## Running tests\n\nUse python -m pytest.\n")
    monkeypatch.setattr(runtime, "DEFAULT_MEMORY_DIR", memory_dir)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Use uv, not pip.\n")
    settings = {**DEFAULT_THREAD_SETTINGS, "memory": True}

    for question in ("what does this define?", "and again?"):
        await runtime.run_turn(
            thread_dir=tmp_path / "thread",
            workspace_root=workspace,
            question=question,
            settings=settings,
            provider_name="ollama",
            base_url=stub_url,
            model="m",
            api_key=None,
            mcp_servers=[],
            broker=ApprovalBroker(),
            emit=lambda _event: None,
        )

    notes = [m["content"] for m in served_requests[-1]["messages"] if m["role"] == "developer"]
    assert sum(DATA_PREAMBLE in note for note in notes) == 1
    assert sum("Use uv, not pip." in note for note in notes) == 1


async def test_a_follow_up_message_does_not_repeat_the_skill_catalog(
    tmp_path: Path, stub_url: str, served_requests: list[dict]
) -> None:
    """The skills watcher's half of the test above, found when chapter 18 was
    rewritten: the same resume fix reached `__main__` and, again, not this
    file. A thread whose skills are switched off afterwards is told so once.
    """
    from minicodex.skills import DEFAULT_SKILLS_DIR, SKILLS_WITHDRAWN
    from minicodex.web import runtime

    workspace = tmp_path / "ws"
    skill_dir = workspace / DEFAULT_SKILLS_DIR / "run-tests"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: run-tests\ndescription: how this project runs its tests\n---\n\n"
        "uv run pytest\n",
        encoding="utf-8",
    )
    on = {**DEFAULT_THREAD_SETTINGS, "skills": True}
    off = {**DEFAULT_THREAD_SETTINGS, "skills": False}

    for question, settings in (("one?", on), ("two?", on), ("three?", off), ("four?", off)):
        await runtime.run_turn(
            thread_dir=tmp_path / "thread",
            workspace_root=workspace,
            question=question,
            settings=settings,
            provider_name="ollama",
            base_url=stub_url,
            model="m",
            api_key=None,
            mcp_servers=[],
            broker=ApprovalBroker(),
            emit=lambda _event: None,
        )

    notes = [m["content"] for m in served_requests[-1]["messages"] if m["role"] == "developer"]
    assert sum("<skill-catalog>" in note for note in notes) == 1
    assert notes.count(SKILLS_WITHDRAWN) == 1
```

---

## §11 那三个"只被第 15 章改过"的文件

`core-diff` 报告里有这么一行：

```
     3 changed only by chapter 15's fixes: registry.py, replay.py, subagent.py
```

（第一版是四个，还有 `agent.py`；后来的改写把同样的修复也打进了第 18 章的快照，见 §1.2 的说明。）

这一章的阅读顺序在第 15 章**之前**，所以它本来不该有第 15 章的修复。但其中一条是这样的（F15-02），`subagent.py` 第 10 章写的那一行原来是 `except TimeoutError:`，现在是：

```python
# `asyncio.TimeoutError` and not the builtin, and it is the *portable*
# spelling rather than the old one: since 3.11 the two are the same object,
# and on 3.10 -- which `requires-python` promises -- they are unrelated
# classes, so `except TimeoutError` never fires there.  A hanging child was
# therefore not stopped at all on the oldest interpreter this package
# claims to support: F10-07's whole defence, off, on a promised platform.
# `mcp.py` had it right in chapter 9 and the knowledge did not travel;
# chapter 15 found it by running the suite on the floor of its own
# metadata for the first time.
except asyncio.TimeoutError:
```

`asyncio.wait_for` 抛的是 `asyncio.TimeoutError`。在 3.11+ 这两个是同一个对象；
在 **3.10——也就是 `requires-python` 承诺的地板——它们是毫无关系的两个类**。
所以子 Agent 的超时防御在这个项目承诺支持的最老解释器上，**完全没生效**。

选择是：为了让 `core-diff` 的数字好看，发一个带已知 bug 的控制台，
还是把这些修复搬过来、然后在报告里如实分成两栏。

搬过来，分两栏。**一个为了让指标好看而保留的 bug，是指标在指挥工作。**


`registry.py` 是同一个修复的三处；`replay.py` 是另一条（读一个第 12 章之前录的文件时，缺的 `attempt` 字段当 0 处理，而不是抛 `KeyError`）：

```python
# `attempt` arrived with chapter 12's retry loop.  A recording made
# before it has no such key, and the *meaning* of its absence is
# not in doubt: there was one attempt per turn and this is it.
# So it is migrated to 0 rather than refused -- which is the whole
# distinction chapter 15 makes about old files.  Refuse when the
# information is gone (a tool call recorded without its arguments,
# below); migrate when it was merely never written down.
#
# It was `payload["attempt"]` until a chapter 5 recording was read
# by this code for the first time, seventeen chapters later, and
# produced a bare `KeyError: 'attempt'` traceback.
key = (payload["turn"], payload.get("attempt", 0))
```

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


> **这一节的数字来自第一次写这一章时的真实运行**（`gpt-4o-mini`，12 次请求）。这次改写手边没有 API key，没有重跑。

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

| 文件 | 行数 | 代码在哪一节 |
|---|---|---|
| `routes.py` | 612 | §3.2、§4.2、§6.1、§10.1、§10.4、§10.5 |
| `runtime.py` | 526 | §2.6、§2.7 |
| `store.py` | 292 | §2.5、§10.3 |
| `channel.py` | 180 | §6.1、§6.2 |
| `approver.py` | 167 | §4.1、§4.3 |
| `app.py` | 151 | §2.5 |
| `events.py` | 71 | §9.1 |
| `__init__.py` | 20 | §2.5 |
| **合计** | **2019** | |

前端，`frontend/src/`：

| 文件 | 行数 | 代码在哪一节 |
|---|---|---|
| `components/Extensions.tsx` | 656 | §7.9 |
| `theme.css` | 627 | **没有贴**：上一版控制台的设计，一行没改，原样搬过来（§7.9） |
| `useThread.ts` | 415 | §7.5、§7.11 |
| `App.tsx` | 288 | §7.7 |
| `console.css` | 263 | §7.9 |
| `components/Chat.tsx` | 250 | §7.6 |
| `components/Sidebar.tsx` | 195 | §7.8 |
| `components/ThreadSettings.tsx` | 185 | §10.6 |
| `api.ts` | 148 | §7.4 |
| `types.ts` | 139 | §7.3 |
| `main.tsx` | 13 | §7.2 |

还有 `frontend/` 下的 `package.json`、`tsconfig.json`、`vite.config.ts`、`index.html`（§7.2）。`package-lock.json` 是 npm 自动生成的，不贴。

工具与测试：

| 文件 | 行数 | |
|---|---|---|
| `tests/test_faults_ch19.py` | 1230 | 54 个测试，全部不联网，每一个都在正文里 |
| `probe_web.py` | 466 | 五段；`browser` 一段贴在 §7.10，其余只引了输出 |
| `probe_mutations_ch19.py` | 385 | 43 条变异（§14） |
| `scripts/check_layers.py` | 383 | 改动的部分在 §8 |

改动的旧文件：`__main__.py`（+36，§2.5）、`pyproject.toml`（`web` 组，§2.5）、`tests/test_packaging.py`（§2.5）、`.github/workflows/ci.yml` 和 `postmerge.yml`（§15）、`registry.py` / `replay.py` / `subagent.py`（第 15 章的修复，§11）。

---

## §14 验证

```
$ uv run ruff format --check .
110 files already formatted

$ uv run ruff check .
All checks passed!

$ uv run python scripts/check_layers.py --quiet && echo ok
ok

$ uv run pytest
1880 passed, 34 warnings in 91.35s (0:01:31)
```

（这次在 Linux 上跑，全绿。第一版是在 Windows 上跑的，有 3 个假设 POSIX shell 的用例在那里会红，和本章无关。）

```
$ npm --prefix frontend run build
> minicodex-console@0.0.1 build
> tsc --noEmit && vite build
vite v7.3.6 building client environment for production...
transforming...
✓ 36 modules transformed.
rendering chunks...
computing gzip size...
dist/index.html                   0.70 kB │ gzip:  0.43 kB
dist/assets/index-DWumoQqC.css   23.75 kB │ gzip:  5.06 kB
dist/assets/index-B4OYWZY4.js   231.18 kB │ gzip: 71.25 kB │ map: 1,029.77 kB
✓ built in 955ms

$ uv run python probe_mutations_ch19.py
43 mutations, tests/test_faults_ch19.py tests/test_packaging.py tests/test_boundaries.py

    1 test(s) fail  <-  events are fanned out as independent tasks again, so order is luck
    1 test(s) fail  <-  the stop sentinel is ignored, so a thread's queue outlives its sockets
    1 test(s) fail  <-  the empty socket set is left behind on the last unsubscribe
    2 test(s) fail  <-  a socket that raises takes the whole pump down with it
    1 test(s) fail  <-  an approval is resolved straight from whichever thread answered
    1 test(s) fail  <-  a second answer to the same approval is accepted
    1 test(s) fail  <-  the approval timeout is caught under the 3.10 spelling that never fires
    1 test(s) fail  <-  a timed-out approval comes back approved
    3 test(s) fail  <-  the busy flag is claimed inside the task again, not before it
    1 test(s) fail  <-  settings can be edited underneath a turn that has already read them
    1 test(s) fail  <-  a relative workspace path is accepted
    1 test(s) fail  <-  a workspace that does not exist is accepted and created later
    1 test(s) fail  <-  a regular file is accepted as a workspace
    1 test(s) fail  <-  an unknown remember scope reaches the rule store
    1 test(s) fail  <-  the api key is echoed back to the browser
    1 test(s) fail  <-  an invented sandbox mode is accepted and lands on an unpredictable default
    1 test(s) fail  <-  an invented approval policy is accepted
    2 test(s) fail  <-  memory reading is on by default -- F17-11, in a browser
    3 test(s) fail  <-  memory writing is on by default
    1 test(s) fail  <-  a session starts with more permission than the CLI gives it
   10 test(s) fail  <-  the settings file is written in place, so a crash mid-write truncates it
    1 test(s) fail  <-  an unreadable json file takes the server down on start-up
    2 test(s) fail  <-  marks are written to disk but never reach the browser
    1 test(s) fail  <-  a permission escalation mid-turn is not reported
    1 test(s) fail  <-  the plan is only reported once the turn is over
    1 test(s) fail  <-  an unchanged plan re-announces itself after every history item
    1 test(s) fail  <-  the console's system prompt loses the skills paragraph
    1 test(s) fail  <-  the console's system prompt loses the memory paragraph
    1 test(s) fail  <-  the volatile permissions block moves off the end of the cached prefix
    2 test(s) fail  <-  the layer checker goes back to seeing one level, and misses web/ entirely
    1 test(s) fail  <-  relative imports stop counting as edges again
    1 test(s) fail  <-  every module becomes an entry point, so the forbidden edge means nothing
    2 test(s) fail  <-  every streamed item gets the same index, so the browser cannot skip a replay
    1 test(s) fail  <-  a compaction's baseline is numbered after the history it replaced
    2 test(s) fail  <-  a pending request is not remembered, so a reload cannot show it
    1 test(s) fail  <-  one thread is shown another thread's pending approvals
    1 test(s) fail  <-  the thread snapshot leaves out what the turn is waiting on
    1 test(s) fail  <-  a turn's approver does not know which thread is asking
    1 test(s) fail  <-  the last socket out leaves its queue for the next socket to join
    1 test(s) fail  <-  a switch spelled "false" is switched on
    1 test(s) fail  <-  the console's writer report catches four types again, not chapter 17's Exception
    1 test(s) fail  <-  a writer that fails during the grace wait escapes before it is reported
    1 test(s) fail  <-  the browser draws replayed items a second time

every mutation was caught.
```

**43 条变异，全部被抓住。** 最后 11 条是这次改写加的，一个修复至少一条。其中三条改的是 `scripts/check_layers.py` 自己——第一版就有，这是这本书第一次去变异一个**检查**而不是代码：一个补好的检查，如果没有东西会注意到它退化，它就在等着重新坏一次。

写这 11 条变异的时候，有一条在写出来之前就看得出会活下来："`run_turn` 不告诉审批者是哪个会话在问"。§4.3 的前两个测试各自测了一半（等待表按会话分开；路由把它返回出去），都不经过 `run_turn`——把两半接起来的那个参数删掉，它们都还是绿的。所以先补了第三个测试，再跑变异检查。**又是"两半各自都测了，接缝没测"**——这一次是在变异检查跑之前，想着"这一条变异，哪个测试会抓住它"时发现的。

`probe_web.py bundle` 的代价那一栏：

```
  built assets (source maps excluded -- they are not served to a viewer):
    index-B4OYWZY4.js             231,183 bytes
    index-DWumoQqC.css             23,746 bytes
    index.html                        701 bytes
                                  255,630 bytes total
  from 11 source files, 3,179 lines
  node_modules to build it: 69 entries
```

旧控制台是**一个 1523 行的 HTML 文件**，双击就能开，没有构建步骤。新的是一个打包产物、十几个源文件、几十个 `node_modules` 顶层目录、一条新的环境要求。换来的是类型检查（它在第一次构建就抓到了三条故障，见回头看）、默认转义（旧版把模型产出的字符串拼进 `innerHTML`）、以及六个新面板还能维护。**这是一笔真实的交易，不是纯赚。**

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

改写时又加了一个：

```
fix(web): stream each item with its place in the thread, resync from the
socket's side, keep a pending approval across a reload

Rewriting the chapter for beginners, and driving the built console in a
real browser for the first time, turned up six faults:

- A resumed turn replays the whole conversation into its new rollout file
  through the observer the console streams from, so the second message
  drew the first exchange again (live 5/5/2 against a reload's 2/3/1).
  history_item now carries its index in the history the file loads back
  as; the browser skips what it already shows. Compaction restarts the
  count at `compacting`, as read_rollout does.
- The optimistic echo was matched only if it was the last entry, and a
  turn often starts with a note: the question was drawn twice.
- First load fetched, then subscribed -- the gap the onopen comment warns
  about. Every open now subscribes first and buffers until the snapshot.
- GET /api/threads/{id} had no pending approvals, so a reload lost the
  card while the turn waited ten minutes. The broker keeps them per thread.
- A socket that subscribed while the last one was leaving joined a pump
  about to stop, and every later event was dropped. A pump now owns its
  queue and socket set; the last one out detaches both.
- web/runtime._finish_writer still had the four exception types chapter
  17's rewrite replaced in __main__, and `bool("false")` is True, so a
  switch spelled as a string was switched on.

Eleven new mutations. Writing the one that drops the thread from run_turn's
approver showed no test went through run_turn, so that test came first.
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
grew 36 lines, all of them the `serve` subcommand.

## Testing


54 tests (up from 40), all offline. 43 mutations, 0 survivors. `check_layers.py`
now covers subpackages, which is how three of this chapter's faults were found.
`probe_web.py browser` drives the built console in a real browser.


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


这次改写找到的 6 条（§0.5）：

| 故障 | 发现 | 修法 |
|---|---|---|
| **续接时把整段历史重新推给浏览器**：`agent._attach` 的重放走的就是写入器的 `append`，浏览器又画一遍。实测第二条消息后 5/5/2，刷新后 2/3/1 | 🟠 | 每个 `history_item` 带上它在读回来的历史里的位置；浏览器跳过已经有的；`compacting` 处从 0 重数（§7.11） |
| **乐观回显只认最后一项**，一轮常以系统提示开头，于是问题被画两次 | 🟠 | 回显带 `pending`，按文字找到并替换（§7.11） |
| **第一次打开页面时先拉快照、后订阅**——正是 `onopen` 注释警告过的那个缝，在每一次页面加载走的路径上 | 🟣 | 每次连上都先订阅、再拉；拉的期间缓冲事件（§7.11）。读出来的，没有复现 |
| **刷新之后审批卡片消失**，这一轮还在等它 10 分钟 | 🟣 | 等待表记住每个请求和它的会话；快照里带上（§4.3） |
| **最后一个 socket 刚走、新的就来**，新的永远收不到事件 | 🟣 | 每个泵拥有自己的队列和集合；最后一个走时立刻摘掉（§6.2） |
| **`web/runtime._finish_writer` 没收到第 17 章改写的修复**；**`bool("false")` 是 `True`** | 🟣 | 两处都接 `Exception`（§2.7）；不是布尔值就拒绝（§10.3） |

还有一条**没修**，指给了第 23 章：记忆面板和"全部忘掉"读的是工作区下面的旧目录（§10.4，F23-05）。

前两条是 🟠（可观测性）：**它们是在浏览器里看见的**——`probe_web.py browser` 是这一章第一次有东西去看"一个人看到了什么"。其余是逐行读代码读出来的。

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


7. **把 `events.py` 里的 `index` 去掉，跑 `probe_web.py browser`。** 第二条消息之后屏幕上有几个"你"？再把 `useThread.ts` 里 `bufferRef` 那几行去掉、只留编号，会发生什么？——提示：编号解决的是"重复"，缓冲解决的是"丢失"，它们不是同一个问题。

8. **（难）让记忆面板读对目录。** 读完第 23 章 §9 再做：把 `get_memory`、`get_memory_jobs`、`forget_all_memory` 改成和 `runtime.load_feature_dirs` 用同一个目录，并把 `test_F19_11_everything_it_learned_can_be_erased_from_where_it_was_enabled` 改成一个对着坏代码会红的测试。

下一章换一个方向：控制台能跑命令了，可"能不能跑"一直是一个**判断**（第 5 章的审批），不是一道**墙**。第 20 章给它一个真正的沙箱。

---

# 附录 · 对照检查

## T1 · 每段代码是在哪一节写出来的

后端：

| 文件 | 内容 | 小节 |
|---|---|---|
| `web/__init__.py` | 全文 | §2.5 |
| `web/store.py` | 说明、常量、`_new_id`、`validate_settings`、`Store` 全部方法 | §2.5（`validate_settings` 的最后一段：§10.3） |
| `web/app.py` | 全文 | §2.5 |
| `web/runtime.py` | 说明、导入、`_instructions`、`load_feature_dirs`、`run_turn`、`_plan_payload`、`_derived`、`resolve_api_key` | §2.6 |
| `web/runtime.py` | `_finish_writer` | §2.6、§2.7 |
| `web/approver.py` | 全文 | §4.1（等待表的 `_asked`/`asked`：§4.3） |
| `web/channel.py` | 全文 | §6.1（泵和退订：§6.2） |
| `web/events.py` | 全文 | §9.1（编号：§7.11） |
| `web/routes.py` | 开头、`SendMessage`、`send_message`、`cancel_turn` | §3.2 |
| `web/routes.py` | `ApprovalResponse`、`respond_to_approval` | §4.2 |
| `web/routes.py` | `thread_socket` | §6.1 |
| `web/routes.py` | `_validated_workspace` | §10.1 |
| `web/routes.py` | `get_memory`、`get_memory_jobs`、`forget_all_memory` | §10.4 |
| `web/routes.py` | 其余所有路由 | §10.5 |

前端：

| 文件 | 小节 |
|---|---|
| `package.json`、`tsconfig.json`、`vite.config.ts`、`index.html`、`src/main.tsx` | §7.2 |
| `src/types.ts` | §7.3 |
| `src/api.ts` | §7.4 |
| `src/useThread.ts` | §7.5（改写的部分：§7.11） |
| `src/components/Chat.tsx` | §7.6 |
| `src/App.tsx` | §7.7 |
| `src/components/Sidebar.tsx` | §7.8 |
| `src/components/Extensions.tsx`、`src/console.css` | §7.9 |
| `src/components/ThreadSettings.tsx` | §10.6 |
| `src/theme.css` | 不贴，从快照复制（§7.9） |

改动的旧文件：

| 文件 | 改了什么 | 小节 |
|---|---|---|
| `pyproject.toml` | `web` 可选组 | §2.5 |
| `__main__.py` | `CONSOLE_DATA_DIR`、`serve` 子命令和分发 | §2.5 |
| `tests/test_packaging.py` | 每个依赖组都要有下界 | §2.5 |
| `scripts/check_layers.py` | 禁止边、`TOP_LEVEL_ENTRY_POINTS`、`iter_modules`、相对导入、四个检查改用 `iter_modules` | §8 |
| `subagent.py`、`registry.py`、`replay.py` | 第 15 章的修复 | §11 |
| `.github/workflows/ci.yml`、`postmerge.yml` | `npm ci`、类型检查、构建、变异检查 | §15 |

`tests/test_faults_ch19.py` 的每一个测试都在正文里：F19-01 在 §3，F19-02 在 §4，F19-03/04 在 §6.1，F19-05 在 §7.11，F19-06/07/08 在 §8，F19-09 在 §9，F19-10/11 在 §10.6，其余的在它们所讲的那一节（§2.5、§2.6、§2.7、§4.3、§6.2、§7.11、§10.3、§10.5）。

## T2 · 常见报错对照表

| 你看到的 | 多半是因为 | 去哪看 |
|---|---|---|
| `minicodex serve` 说 `the console needs its extra` | 只装了基础依赖 | `uv sync --all-extras`（§2.5） |
| 打开 8000 端口看到 "The console has no build yet" | 前端没构建 | `npm --prefix frontend ci && npm --prefix frontend run build`（§0.4） |
| `npm run dev` 页面能开，但发消息 404 | 后端没起，或者起在别的端口 | 另开一个终端 `uv run minicodex serve`；代理写死在 `vite.config.ts` 里指向 8000（§7.2） |
| `npm run dev` 下 WebSocket 连不上，HTTP 却正常 | `vite.config.ts` 的 `/ws` 那条少了 `ws: true` | §7.2 |
| 发消息回 `400 no active model provider` | 没有启用任何提供方 | 扩展页 → Providers，点一个设为当前（§2.5 的 `seed_providers`） |
| 发消息回 `409 still answering` | 这个会话上一轮还没完 | 等它完，或者点 Stop（§3） |
| 审批点了没反应，600 秒后显示超时 | 路由被写成了 `def` | 改成 `async def`（§4） |
| 刷新页面后审批卡片不见了，会话一直在"思考" | 快照里没有正在等的审批（改写前的代码） | §4.3 |
| 发第二条消息后，第一轮的对话又出现了一遍 | 续接的重放被当成新内容画了（改写前的代码） | §7.11 |
| 自己的问题在屏幕上出现两次 | 乐观回显没被替换（改写前的代码） | §7.11 |
| 对话流偶尔顺序不对 | 用了 `create_task` 扇出 | §5。注意在笔记本上大概率复现不出来 |
| 服务器 reload 之后页面卡在 `thinking…` | 没有 `onclose`/重连 | §7 |
| 页面显示已连接（`live`），却再也收不到事件 | 一个 socket 刚断开、新的紧接着连上（刷新、第二个标签页、开发模式的 `StrictMode`），撞上泵的退订竞态（改写前的代码） | §6.2、§7.2 |
| 记忆面板是空的，可 Agent 明明记得东西 | 面板读的是工作区下面的旧目录 | §10.4（第 23 章 F23-05 修） |
| `400 memory must be true or false` | 开关写成了字符串 | §10.3 |
| `check_layers.py` 报 `no such file` 之类的怪错 | 在错误的目录跑 | 它默认 `src/minicodex`，要在 step 根目录跑 |
| 加了新模块之后 `check_layers.py` 还是全绿 | **可能是它根本没看见**（F19-06） | 故意加一条禁止边试试它会不会红（§8） |
| `test_F_1_02` 突然红了 | 新加的依赖没写下界 | 每个依赖都要 `>=`，**每个可选组都要**（§2.5） |
| `pytest` 里 WebSocket 测试挂住 | `TestClient` 没用 `with` | `with TestClient(app) as client:`，否则启动钩子不跑（§3.1） |
| 前端 `tsc` 报 `Property 'text' does not exist` | 在联合类型上用了 `Omit` | 用分配式条件类型（§7.5 的 `Unkeyed`） |
| 改了 `agent.py` 里标记的名字，测试红了 | 这是设计如此 | 报错信息会告诉你去更新 `test_F19_09_...`（§9） |
| `probe_web.py browser` 说 `could not drive a browser` | 没装 Node 或 Playwright | §0（环境要求） |
