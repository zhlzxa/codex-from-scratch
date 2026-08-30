# 第 22 章 · 控制台里连一个服务器：远程 MCP 与 GitHub 的引导 UI

> **代码**：`steps/step22_mcp_console/`
> **产出**：`src/minicodex/web/oauth.py`（浏览器中介的 OAuth，324 行）、
> `src/minicodex/web/mcp_config.py`（一条记录 → 一个 config，94 行）、
> `routes.py` +206 行（两种表单形状、三个新路由）、`runtime.py` +70 行
> （按形状分派）、`Extensions.tsx` +356 行、`mcpOauth.ts`（72 行）。
> **你需要**：全部离线跑。真连 GitHub 需要你自己的凭据，这一章会说清楚
> 需要哪一种、以及哪一半这里测不了。
>
> **阅读顺序和编号都在第 21 章之后。这是全书最后一个单元。**

---

## §1 一个只有库有、用户没有的功能

第 21 章把远程 MCP 和 OAuth 做进了库里，测得很扎实：36 个测试、11 条变异全中、
对着真实的 GitHub 和 DeepWiki 量过延迟。

然后有人问了一句：**前端能连吗？**

不能。控制台的"添加服务器"表单从第 9 章起就只问一件事——一条命令：

```tsx
// step21 的 Extensions.tsx，MCP 那一页
const [form, setForm] = useState({
  name: "", command: "", env: "", cwd: "",
  startup_timeout: "30", tool_timeout: "60",
});
```

没有 `url`。后端 `McpIn` 也只有 `command`。而 `runtime.py` 里，每一条存下来的
记录都会被无条件地造成一个 `ServerConfig`：

```python
configs = [
    ServerConfig(name=s["name"], command=tuple(s["command"]), ...)
    for s in enabled
]
```

`s["command"]` —— 一个远程服务器的记录里根本没有这个键。这段代码不是"不支持
远程"，是**假设远程不存在**。

所以第 21 章之后，这个项目的真实状态是：一个能连 GitHub 的库，和一个连不到
GitHub 的产品。**一个只能从库里调用、在人们真正会用的那个界面上够不着的能力，
不算已经交付。**

## §2 但这不是"给表单加个 url 字段"

如果问题只是表单少了一个输入框，这一章会很短。它不短，因为第 21 章的 OAuth
是**照终端写的**。

看一眼那两个 handler 的形状：

```python
# 第 21 章 mcp_oauth.py：CLI 的形状
async def redirect_handler(url: str) -> None:
    print(f"open this: {url}")
    webbrowser.open(url)          # ← 在这台机器上开浏览器

def make_loopback_callback(port: int):
    async def callback() -> AuthorizationCodeResult:
        # ← 在这台机器上监听一个回环端口，阻塞到那一个请求进来
        ...
```

两个都假设**发起授权的进程和完成授权的浏览器在同一台机器上**。终端里这是天经
地义的：你在自己的笔记本上敲 `minicodex`，弹出来的浏览器也是你自己的。

Web 控制台不是这个形状。agent 跑在服务器上，浏览器在读这段话的人那里，两边
之间只有控制台已经在监听的那个端口。这个假设**两个方向同时破掉**：

- 服务器**开不了**别人的浏览器。它没有屏幕。
- 别人的浏览器**够不着**服务器的 `127.0.0.1:1456`。那是服务器的 localhost，
  不是用户的。

这就是 **F22-01**，也是这一章存在的理由。

**一次阻塞调用要变成两次 HTTP 请求，中间隔着一个人。** 而第一次请求学到的
所有东西——发现出来的端点、注册好的 client、生成的 PKCE verifier——都得活到
第二次请求到达，那可能是几分钟以后，甚至是这个进程的另一个线程上。

## §3 拆成两个路由

```
POST /api/mcp/oauth/start
    → 发现 protected-resource metadata（RFC 9728）
    → 发现 authorization-server metadata（RFC 8414）
    → 动态注册，或用预置的 client_id（RFC 7591）
    → 生成 PKCE（RFC 7636）
    → 存进 PendingOAuthTable，键是一个服务端生成的 state
    ← 返回 authorize_url（不打开它）

  [ 浏览器新标签页 → 授权服务器 → 用户点"同意" → 重定向回来 ]

GET /api/mcp/oauth/callback?code=...&state=...
    → 用 state 找回那次 pending
    → 换 token
    → 写进 OwnerTokenStorage
    ← 一个"连上了 / 没连上"的 HTML 页面
```

关键的一句：**RFC 7591、8414、7636 一个字都没有重新实现。**
`web/oauth.py` 直接驱动 SDK 自己的积木——`OAuthContext` 拿请求/响应形状、
`PKCEParameters.generate()` 生成挑战、几个 free function 做发现和注册。

新的只有**那道缝**：`OAuthClientProvider.async_auth_flow` 是一个生成器，只在
一次活着的 401 里从头跑到尾，中间那次阻塞的 `callback_handler` 是它的一部分。
把那条线剪断、让一个 HTTP 请求坐进去——这件事 SDK 没有替你做，因为它不知道
你的浏览器在哪。

跑一次看看真实的输出（对着离线的假授权服务器）：

```
stored record:
  kind = 'remote'
  name = 'gh'
  url = 'http://127.0.0.1:13005/protected'
  bearer_token = None
  oauth = {'client_id': 'preset-client', 'scope': None}

authorize_url query:
  response_type          code
  client_id              preset-client
  redirect_uri           http://console.local/api/mcp/oauth/callback
  code_challenge_method  S256
  state                  1v5-bvcFBXED...  (43 chars)
  code_challenge         FmX_kDlX0Ke8...  (43 chars)
```

注意 `redirect_uri`：`http://console.local/api/mcp/oauth/callback`。
那个主机名不是配置出来的，是 `request.base_url` ——**谁应答了这次 start 请求，
授权服务器的重定向就得落回谁那里**。本地开发是 `localhost:8765`，部署到
真实域名就是那个域名，同一条存储记录不用改，也没有一个"公开 URL"设置项
等着被填错。

## §4 F22-02：`state` 是查出来的，不是信来的

回调路由手上只有一样东西：浏览器带回来的查询字符串。而那是**用户可以随便编的**。

如果 `state` 只是个用来"标记这是哪台服务器"的标签，那就是一扇敞开的门：
猜中或重放一个 state，就决定了一个授权码会被换成**哪个服务器的**凭证。

所以规则是：`state` 由服务端 `secrets.token_urlsafe` 生成，而且**只被查，
从不被信**。

```python
def pop(self, state: str) -> PendingOAuth | None:
    """The one lookup the callback route is allowed to trust (F22-02).

    A `state` that is not a key in this table -- expired, never issued, or
    typed by hand -- returns `None` and nothing about the request is
    believed past that point. Popped rather than merely read: a state is
    good for exactly one callback, the same way an authorization code
    is good for exactly one token exchange.
    """
    self._sweep()
    return self._table.pop(state, None)
```

三件事值得单独说：

**一、查不到就什么都不做。** 未知的、过期的、根本没带 `state` 的，得到的是
同一句话："this authorization link is unknown or has expired"。路由不会因为
你猜得"接近"就多说一个字——它不泄露哪些 state 存在。

**二、`pop` 不是 `get`。** 一个 state 只够一次回调。这和授权码只能换一次
token 是同一条规则，而且必须自己实现——授权服务器会拒绝第二次换码，但
"拒绝"是一次多余的往返，而且要等到那时候才知道。

**三、下游一个字都不从查询字符串里读"这是哪台服务器"。** 那个信息来自
pending 条目，是服务端自己写进去的。

测试直接把这条钉死：

```python
first = await api.get("/api/mcp/oauth/callback", params=query)
assert "connected to gh" in first.text
assert auth.token_calls == 1

second = await api.get("/api/mcp/oauth/callback", params=query)
assert "unknown or has expired" in second.text
assert auth.token_calls == 1, "the code must not be exchanged twice"
```

一个人双击了一个过期链接、一个浏览器重放了请求——`token_calls` 都还是 1。

## §5 F22-03：点了"连接"然后关掉标签页的人

`start` 之后 `callback` 之前，那条 pending 记录里有 code verifier、有发现出来
的端点。如果那个人想了想、把标签页关了呢？

那条记录会一直待到进程重启。一个只增不减的表，是那种跑三个月才有人注意到的
问题——**🔵 长期运行**那一类。

十分钟 TTL，而且是**懒清扫**：在下一次 `put`/`pop` 时顺手扫，不开后台任务。

```python
def _sweep(self) -> None:
    now = time.time()
    expired = [state for state, p in self._table.items() if now - p.created_at > self._ttl]
    for state in expired:
        del self._table[state]
```

代价是两次清扫之间可能多留一条过期条目。这个取舍第 17 章的 `JobStore` 已经
做过一次，理由一样：**没事发生的时候，少一个东西在跑。**

十分钟这个数字是按"一个人读完一屏真实的授权同意页"定的，不是按机器定的。

## §6 F22-04：既不是这个形状、又是那个形状的记录

`add_mcp` 原来会高高兴兴地存下一条同时有 `command` 和 `url` 的记录，或者两个
都没有的记录。问题要等到下一轮真去连的时候才炸——**晚了一轮，而且离产生它的
那个表单隔了一层。**

现在在门口就拒绝，而且说的是那个人正在看的表单里的字段名：

```
command + url  -> 400  a server is reached one way or the other -- set 'command' or 'url', not both
neither        -> 400  give this server either a command (stdio) or a url (remote)
token + oauth  -> 400  a server is authenticated one way or the other -- a bearer token or OAuth, not both
```

`mcp.load_config` 对配置**文件**已经做过同一条校验了（F21-01）。这里是故意做
第二遍，因为两层的失败后果不一样：**库的职责是对调用它的代码抛异常，路由的
职责是告诉一个人该改哪个输入框。**

同样的重复也用在认证那一对上——路由拒绝一次，`server_config` 在把记录变成
真能连的东西时再拒绝一次，防的是一条通过别的路径进到存储里的记录，比如有人
手工编辑了 `mcp.json`。

## §7 token 存哪，以及一件要说实话的事

一个人在表单里粘进来的 bearer token，不走 `bearer_token_env_var`，直接折成一个
`Authorization` 头。

第 21 章设计那个字段是为了让配置**文件**不必明文存密钥——那个理由对 CLI 读的
`mcp.json` 现在也还成立。但控制台的存储不是那个文件：它是一个浏览器写出来的
本地运行状态，和第 19 章起 `providers.json` 里的 `api_key` 完全一样，就按同样的
方式对待。

出去的路上会脱敏：

```
stored, then read back through GET /api/mcp:
  POST reply bearer_token = True
  GET  reply bearer_token = True
  on disk                 = 'ghp_a_real_looking_secret'
```

`True` 的意思是"有一个"，不是那个值。这条规则第 19 章给 provider 的 API key
划过一次（`_public`），这里是同一条线画到新字段上——**所以它不是一条新故障，
只是一条已有规则的正确适用**，故障清单里不该给它编号。

而 `on disk` 那一行是明文的。这是实话，也是这一章要说出来的话：控制台的存储
就是明文的，一直如此，第 19 章的 API key 也是。**这不是这一章新引入的问题，
但这一章多存了一种密钥进去，所以要在这里再说一次**，而不是等第 23 章做多租户
的时候才发现有两处明文而不是一处。

## §8 前端为什么是轮询，不是 `postMessage`

"连接"按钮点下去之后，浏览器要怎么知道那个新标签页里的事办完了？

直觉答案是 `postMessage`：回调页面拿到 `window.opener`，喊一声。

**做不到，而且理由很好。** 实际调用的是：

```ts
window.open(url, "_blank", "noopener")
```

`noopener` 不是顺手加的：它意味着新标签页**没有指回来的引用**，也就没有
`postMessage` 的通道。选 `postMessage`，等于选了一个"只有在你同时做一件更不
安全的事情时才成立"的机制。

所以是轮询 `GET /api/mcp/{id}/oauth/status`。这个选择还顺带扛住了三件
`postMessage` 扛不住的事：**弹窗被拦截、标签页被提前关掉、页面被刷新。**
后端的回调路由本来就是照"让 token 出现在那个 status 里"写的，不是照"回头喊
一嗓子"写的。

`runOAuthConnect` 写成一个所有副作用都注入进来的普通 async 函数，不是 hook，
这样 `mcpOauth.test.ts` 可以拿假的 `openTab` 和零延迟的 `sleep` 驱动它——
不需要真弹窗，也不需要真时钟：

```
 ✓ src/mcpOauth.test.ts (4 tests) 30ms

 Test Files  1 passed (1)
      Tests  4 passed (4)
```

## §9 一个没有浏览器的进程不能假装有

回到 F22-01 的另一半。一轮对话开始时，`runtime.py` 要把存下来的记录变成
`connect()` 要的 config 列表。如果有一台远程服务器配了 OAuth 但**还没连过**呢？

朴素的做法是照连不误，让它失败。但失败在哪里？在 SDK 的 401 分支里，
那里会去调 `redirect_handler`——而一个跑在服务器上的进程能传进去的
`redirect_handler` 只有一个会抛异常的版本。

所以这一轮**明确地不连它**，并且说人话：

```python
if record_kind(s) == "remote" and config.oauth is not None:
    storage = OwnerTokenStorage(DEFAULT_OWNER, server=config.name)
    if await storage.get_tokens() is None:
        emit({
            "type": "mcp_status", "name": config.name,
            "status": "needs_connection",
            "detail": "not connected yet -- use Connect in the MCP tab",
        })
        continue
```

这不是一条新故障，是 F22-01 的同一个事实换个位置出现：**一个没有浏览器的
进程不能假装它能给人看一个页面。** 能做的是说清楚该去点哪里。

## §10 F22-05 没有了

计划里写了一条 F22-05：`mcp_status` 要不要区分"配置了但没连接"和"连接了但
没用过"。

做完这一章，它自己回答了：前半句就是 §9 那个 `needs_connection`，是架构的
副产品而不是一条独立的发现；后半句是一个使用率问题，而这一章没有任何一次
测量在问它。

**计划里的编号，做出来不成立就删掉，不要为了凑数把它写成一条故障。**
这本书的故障编号能反查，是因为每一条背后都有一次真的踩坑；给一条没踩过的坑
编号，等于往那个索引里掺沙子。

## §11 GitHub 到底连不连得上——分两半说

这一章是被"至少做 GitHub 的连接"这句话推动的，所以结论必须诚实到分两半：

**能用的那一半：粘一个 personal access token，今天就能连。** 远程表单里选
"paste a token"，把 PAT 贴进去，没有流程、没有浏览器、没有任何要注册的东西。
这条路第 21 章已经量过（F21-07），这一章只是把它接到了表单上。

**要先做一步人工的那一半：交互式 OAuth。** GitHub 的授权服务器**不广播
`registration_endpoint`**——这是第 21 章对着真实 GitHub 量出来的
（F21-08b），不是猜的。没有那个端点，SDK 的动态客户端注册就没有地方可调。

所以要走完整 OAuth，**运行这个控制台的人得先自己去 GitHub 注册一个 OAuth
App**，拿到 client id 填进表单。这一步这个按钮消不掉。它能做的是：**让这一步
之后的每一步都变成一次点击。**

表单里那段说明就是这么写的，不是"连接 GitHub"四个字然后祝你好运：

> Dynamic client registration (RFC 7591) does not work against every
> authorization server -- GitHub's does not advertise it. Leave client ID
> blank to try dynamic registration; for a server like GitHub's, its operator
> first registers an OAuth App and puts the client ID here.

**这一章的真实验证边界**：完整的 `start → 授权 → callback → token 落盘` 链路
是对着本章自己写的假授权服务器跑通的，包括 DCR 支持和不支持两种形状。
**对真实 GitHub 的完整 OAuth 没有跑过**——这个环境里没有一个注册好的
GitHub OAuth App，也没有去要一个。拿离线的绿灯冒充"GitHub 也连通了"，
是这本书从第 14 章起就一直在防的那种谎。

## §12 codex 在这一章里没有对照

前面二十一章每一章末尾都有一节"对照 codex"。这一章没有，而且不是因为忘了。

**codex 是 CLI 和 TUI，它没有 Web 控制台。** 所以它没有浏览器中介的 OAuth、
没有 pending 表、没有服务端渲染的回调页面——这些东西在 `codex-rs` 里没有对应物。

能对照的部分都是第 21 章的：远程服务器配置的字段形状
（`codex-rs/config/src/mcp_types.rs:274-291`）、预置 `client_id` 的姿态
（`rmcp-client/src/perform_oauth_login.rs`）。那些引用还成立，直接复用，
不在这里重述一遍。

**该说"codex 不做这个"的时候就这么说**，不要硬找一段 `codex-rs` 里形状相近
但其实不是一回事的代码来凑一个对照。这本书的对照有价值，是因为它每次都真的
指着同一个东西。

## §13 清点：这一章写了多少代码

| 文件 | 行数 | 干什么 |
|---|---|---|
| `src/minicodex/web/oauth.py` | 324 | `PendingOAuthTable`、`begin()`/`finish()` |
| `src/minicodex/web/mcp_config.py` | 94 | 一条记录 → 一个 config，两处调用共用 |
| `web/routes.py` 的改动 | +206 | 两种表单形状、三个新路由、脱敏 |
| `web/runtime.py` 的改动 | +70 | `resolve_mcp_configs`，按形状分派 |
| `frontend/.../Extensions.tsx` | +356 | 本地/远程切换、两条认证路径、Connect GitHub |
| `frontend/src/mcpOauth.ts` | 72 | 轮询式连接流程，副作用全注入 |
| `tests/test_faults_ch22.py` | 497 | 24 个测试，含两条端到端链路 |
| `frontend/src/mcpOauth.test.ts` | 104 | 4 个前端测试，用已有的 vitest |
| `probe_mutations_ch22.py` | 173 | 12 条变异 |

`mcp_config.py` 那 94 行值得单说一句：它只有两个函数，被 `routes.py` 和
`runtime.py` 各调一次。放在这里而不是塞进其中任何一个，是因为**两次是同一个
翻译，而写两遍的翻译一定会漂移**——第 3 章为提示词和 schema 里重复的工具描述
做过一模一样的修复。

## §14 验证

```bash
uv run ruff format . && uv run ruff check .      # 125 files, All checks passed!
uv run pytest -q                                  # 1881 个测试，全绿
uv run python probe_mutations_ch22.py             # 12/12 被抓住
uv run python scripts/check_layers.py             # 无环
cd frontend && npm run typecheck && npm test      # tsc 干净，4/4
```

真实的变异输出，一条不落：

```
12 mutations, tests/test_faults_ch22.py

    1 test(s) fail  <-  add_mcp accepts a command and a url together instead of refusing
    1 test(s) fail  <-  add_mcp accepts neither a command nor a url instead of refusing
    1 test(s) fail  <-  add_mcp accepts a bearer token and OAuth together instead of refusing
    1 test(s) fail  <-  a bearer token is echoed back to the browser instead of redacted
    1 test(s) fail  <-  a record's own shape is ignored -- everything dispatches as stdio
    1 test(s) fail  <-  a raw bearer token is never folded into the Authorization header
    1 test(s) fail  <-  a bearer token and OAuth configured together is accepted instead of refused
    2 test(s) fail  <-  an expired pending attempt is never swept -- F22-03's bug
    2 test(s) fail  <-  pop() reads a pending attempt without removing it -- a state becomes reusable
    3 test(s) fail  <-  the callback route trusts an unknown state instead of rejecting it
    1 test(s) fail  <-  resolve_mcp_configs stops checking whether a remote OAuth server has a token
    1 test(s) fail  <-  a server with a token on file is treated as never connected

every mutation was caught.
```

**已知环境失败**：这一步一条都没有。1881 个测试全绿。

跑之前重新数了一遍 skip，一共 12 条，全部是平台原因，而且**没有一条属于
这一章**：

```
7  tests/test_shell.py       F02-10: POSIX 进程组和 POSIX shell 内建
1  tests/test_faults_ch07.py F02-10: killpg 是 POSIX-only
1  tests/test_approval.py    这个平台上建符号链接要权限
2  tests/test_faults_ch20.py 这台机器上没有 bwrap
1  tests/test_faults_ch21.py POSIX 文件权限位；Windows 用 ACL
```

第 22 章自己的 24 个测试**一条都不跳**——它们全都是离线的：假授权服务器是
`asyncio.start_server`，浏览器是 `TestClient` 和一个 `httpx2` 客户端，
没有任何一条解析真实域名。

这段清单是这次跑出来数的，不是从上一章抄的。抄一份旧的"已知失败清单"，
和编造一段终端输出没有本质区别——都是把一个没验证过的断言写成事实。

## §15 收工：commit、PR

```
git checkout -b feat/mcp-console
git add src/minicodex/web/oauth.py src/minicodex/web/mcp_config.py
git add src/minicodex/web/routes.py src/minicodex/web/runtime.py
git add frontend/src/mcpOauth.ts frontend/src/mcpOauth.test.ts
git add frontend/src/components/Extensions.tsx frontend/src/api.ts frontend/src/types.ts
git add tests/test_faults_ch22.py probe_mutations_ch22.py
git commit -m "feat(console): connect a remote MCP server from the browser"
```

### PR 描述

> **控制台里连远程 MCP 服务器，含 OAuth。**
>
> 第 21 章把远程 MCP 做进了库里，但控制台够不着：表单只问命令，`runtime.py`
> 无条件按 stdio 分派。这个 PR 把那条路打通。
>
> 不是加一个 `url` 输入框那么简单。第 21 章的 OAuth 假设浏览器和 agent 在同
> 一台机器上（本地回环回调），服务端托管的控制台两个方向都不成立。一次阻塞
> 调用拆成了两个路由，中间隔一次浏览器往返，用服务端生成、服务端校验的
> `state` 串起来（F22-01/02/03）。
>
> RFC 7591/8414/7636 一行都没重写——驱动的是 SDK 自己的积木，新的只有那道缝。
>
> **GitHub**：PAT 现在就能用。完整 OAuth 还需要运营者先注册一个 OAuth App
> （GitHub 不支持动态注册，F21-08b 量过），表单里写明了。真实 GitHub 的完整
> OAuth 流程**没有**在 CI 里跑过，本地也没有——离线假授权服务器覆盖了链路，
> 不冒充真连接。

### code review

> **R:** `PendingOAuthTable` 是进程内的，重启就没了。要不要落盘？
>
> **A:** 不要。一条 pending 的寿命是"一个人读完授权页"的十分钟，重启之后
> 那个人的浏览器早就跳走了，恢复它救不了任何一次点击。而落盘意味着把
> code verifier 写到磁盘上——为一个救不了的场景，多一处密钥落地。
>
> **R:** `redirect_uri` 从 `request.base_url` 推，会不会被 Host 头伪造？
>
> **A:** 会，但那个洞不在这里。伪造 Host 能让 `redirect_uri` 指向别处，而
> 授权服务器**只接受注册时登记过的 redirect URI**——这正是 OAuth 要求预先
> 登记的原因。伪造一个没登记过的，授权服务器自己会拒。真要防到底，是在
> 反向代理上锁死 Host，那是部署配置，不是这一层的事。第 23 章做多租户的时候
> 会再碰这个面。

## §16 如果你只记住三件事

**一、一个库里有、界面上够不着的能力，不算交付。** 第 21 章测得再扎实，用户
问"能连 GitHub 吗"的答案也还是不能。

**二、把阻塞调用拆成两个请求，是"人在中间"这件事的通用形状。** 一次终端里
的 `input()`、一次 CLI 的回环回调、一次 Web 的授权跳转——同一个问题的三种
部署形态，而只有第三种要求你把"第一次学到的东西"显式地存起来等第二次。
状态从栈上搬到表里的那一刻，`state` 的生成、校验、过期、一次性，全都变成
你自己的责任。

**三、说"这里没有对照"比硬找一个对照诚实。** codex 没有 Web 控制台。承认
这一点，比在 `codex-rs` 里翻出一段形状相近的代码来撑场面有用得多。

## §17 动手练习

1. **把 TTL 调成 1 秒**，跑 `test_F22_03_a_fresh_attempt_survives_the_sweep`。
   它应该红。想清楚为什么这条测试比"过期的会被扫掉"那条更容易写错。

2. **让 `pop` 变回 `get`**（`probe_mutations_ch22.py` 里就有这条变异）。
   两个测试会红。其中一个是"同一个链接用两次"——去看它断言的是
   `auth.token_calls`，不是回调页面的文字。为什么？

3. **加一个 DeepWiki 的快捷按钮**（`https://mcp.deepwiki.com/mcp`，不需要认证）。
   你会发现 §6 的校验挡住了什么：一个既没有 token 也没有 OAuth 的远程服务器
   是**合法的**。想清楚为什么"认证方式"可以为空，而"command 还是 url"不行。

4. **难**：F22-02 的规则是"查不到就什么都不说"。写一个测试，证明一个未知的
   `state` 和一个**过期的** `state` 得到的响应**逐字节相同**。如果不同，
   攻击者能从中读出什么？
