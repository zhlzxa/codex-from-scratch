# 第 22 章 · 控制台里连一个服务器：远程 MCP 与 GitHub 的引导 UI

> **代码**：`steps/step22_mcp_console/`
> **分支**：`feat/mcp-console`
> **产出**：`src/minicodex/web/oauth.py`（浏览器中介的 OAuth）、`src/minicodex/web/mcp_config.py`（一条记录 → 一个 config）、`routes.py` 两种表单形状和三个新路由、`runtime.py` 按形状分派、`Extensions.tsx` 远程表单和 Connect 按钮、`mcpOauth.ts` 轮询式连接流程、`probe_mutations_ch22.py` 20 条
> **前置**：做完第 21 章。本章 33 个后端测试、5 个前端测试，全部离线。真连 GitHub 需要你自己的凭据，§11 说清楚需要哪一种、以及哪一半这里测不了
> **阅读顺序和编号都在第 21 章之后。** 后面还有第 23 章（多用户）

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **Web 控制台的 OAuth**：第 21 章的 OAuth 是给**终端**写的——程序和浏览器在同一台机器上。第 19 章的控制台不是：agent 跑在服务器上，浏览器在用户那里。这一章的全部内容就是这个差别（§2）。
- **回调路由（callback route）**：授权服务器在用户点"同意"之后，把浏览器**重定向**到的那个网址。在这一章里，它是控制台自己的一个路由 `/api/mcp/oauth/callback`。
- **`state` 参数**：OAuth 授权网址里带的一个随机串，授权服务器原样带回来。它是回调路由认出"这是哪一次尝试"的唯一凭据（§4）。
- **待完成表（pending table）**：`start` 学到的东西（PKCE 的 verifier、发现出来的端点）要活到 `callback` 到达——可能是几分钟以后。存在内存里的一张表，键是 `state`（§3、§5）。
- **TTL（time to live）**：一条记录最多活多久。过期就删。
- **懒清扫（lazy sweep）**：不开后台任务定时清理，而是在下一次读写时顺手把过期的删掉（§5）。
- **轮询（polling）**：每隔一会儿问一次"好了没有"。和"等对方通知"相反（§8）。
- **`window.open(url, "_blank", "noopener")`**：在新标签页打开网址，`noopener` 表示新标签页拿不到指回原页面的引用。
- **脱敏（redaction）**：把密钥换成 `true` 再发给浏览器，"有一个"，而不是那个值（§7）。
- **XSS（跨站脚本）**：一段别人控制的文字被当成 HTML 放进你的页面，里面的 `<script>` 或 `onerror=` 就会在**你的网站上**执行，能调用你网站的所有接口（§4.1）。
- **转义（escape）**：把 `<`、`>`、`"`、`&` 换成 `&lt;` 这样的写法，让浏览器把它们当文字显示，而不是当标签执行。
- **指纹（fingerprint）**：一个值的哈希的前几位。两个值不同，指纹几乎一定不同；但从指纹推不回原值（§8.1）。

### 0.2 本章会遇到的新 Python / TypeScript 写法

| 写法 | 意思 |
|---|---|
| `secrets.token_urlsafe(32)` | 32 字节的密码学随机数，编码成可以放进网址的字符串（43 个字符） |
| `html.escape(text)` | 把 `<>&"'` 换成 HTML 实体 |
| `hashlib.sha256(b).hexdigest()[:16]` | SHA-256 哈希，十六进制，取前 16 个字符 |
| `str(request.base_url)` | 这次请求是用哪个"协议://主机:端口/"到达的 |
| `HTMLResponse(text)` | FastAPI：返回一个 HTML 页面而不是 JSON |
| `"{title}".format(title=...)` / `{{` `}}` | 字符串模板；模板里真的要写花括号时写两个 |
| `async with (A() as a, B() as b):` | Python 3.10+：括号里一次进入几个异步上下文 |
| `dict.pop(key, None)` | 取出并删除；没有这个键就返回 `None` |
| `except () as exc:` | 一个空元组——什么异常都不接（只出现在变异里） |
| TS：`deps.sleep ?? defaultSleep` | `??`：左边是 `null`/`undefined` 时用右边 |
| TS：`({ authorize_url: url } = await f())` | 解构赋值：从返回的对象里取出一个字段 |
| TS：`(token ?? null)`、`token === undefined` | 区分"没有这个字段"和"字段是 null" |
| vitest：`describe` / `it` / `expect(x).toBe(y)` | 前端测试：一组、一个、一句断言 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/mcp-console
```

### 0.4 这次改写时找到的

第一版的 24 个后端测试和 4 个前端测试全部是绿的。改写时做了三件第一版没做的事：把每一个测试链路**往后多走一步**（拿到 token 之后，真的用它开一轮对话）；把**授权服务器和 MCP 服务器放在不同的端口上**（真实世界就是这样）；把每一段说明和它的代码对着读。找到了这些：

1. **回调页面有 XSS**：授权服务器在重定向里带回来的 `error_description` 原样当 HTML 放进页面——在控制台自己的网址上。一段脚本在那里可以调用 `POST /api/mcp`，添加一个运行任意命令的服务器（§4.1）。
2. **删掉一个服务器，token 还在**：token 按服务器的**名字**存。删掉 `gh`、再加一个也叫 `gh`、指向别处的服务器——新的那个立刻显示"已连接"，它的第一个请求带着 GitHub 的 token 发给了另一台主机。而且两条记录本来就可以同名（§7.1）。
3. **"重新连接"在用户什么都没做之前就报告成功**：前端只问"有没有 token"，旧 token 还在，第一次轮询就是"有"（§8.1）。
4. **控制台的发现流程不读服务器的 401**：它只去猜 MCP 服务器那台主机上的固定路径。一个只在 401 里说明"去哪认证"的服务器，从命令行连得上，从控制台连不上（§3.1）。
5. **一轮对话会因为一条坏记录整个失败**：`mcp_config.py` 的注释说要防"手工编辑过的记录"，可防的方式是抛异常——抛出 `run_turn`，所有服务器、所有对话都用不了（§6.2）。
6. **一条第 22 章之前存的 stdio 记录在界面上显示成一行空白**：`types.ts` 说前端可以依赖 `kind` 字段"因为后端已经补上了"——后端没有补（§7）。
7. 前端那个"超时"测试注释说它用了假时钟，其实读的是真的 `Date.now()`（§8.2）；`OAuthStartError` 定义了从来没用过（§3）；第一版正文说这是"全书最后一个单元"、说第 21 章有"36 个测试"（是 35 个）。

还有一件事：第一版的端到端测试停在"token 已经写进磁盘"。这一次往后多走一步——用这个 token 真的开一轮——才发现第 21 章的 `McpClient` 根本带不上它（第 21 章改写时修的那个 `_owner` bug）。**第一版的"在控制台里连 GitHub"，连上之后的每一轮对话都会失败。**（§9.1）

---

## §1 一个只有库有、用户没有的功能

第 21 章把远程 MCP 和 OAuth 做进了库里，测得很扎实：35 个测试、11 条变异全中、
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

（上面那两个 handler 是简化后的形状，真代码在第 21 章 §8.3：`default_redirect_handler` 打印网址再试着打开浏览器，`make_loopback_callback` 在 `127.0.0.1:1456` 上开一个只等一次请求的服务器。）

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


跑一次看看真实的输出（对着离线的假授权服务器，控制台的地址假装是 `http://console.local`）：

```
$ uv run python demo22.py
stored record:
  kind = 'remote'
  name = 'gh'
  url = 'http://127.0.0.1:PORT/protected'
  bearer_token = None
  oauth = {'client_id': 'preset-client', 'scope': None}

authorize_url: http://127.0.0.1:PORT/authorize?...
  response_type          code
  client_id              preset-client
  redirect_uri           http://console.local/api/mcp/oauth/callback
  code_challenge_method  S256
  state                  qbgxZBE93tHJ...  (43 chars)
  code_challenge         SW31KgY88zqu...  (43 chars)
  resource               http://127.0.0.1:PORT/protected

previous = None
requests the stub saw: ['POST /protected', 'GET /.well-known/oauth-protected-resource/protected', 'GET /.well-known/oauth-protected-resource', 'GET /.well-known/oauth-authorization-server']
```

（`demo22.py`：用 `httpx2.ASGITransport` 在进程里调用控制台，先 `POST /api/mcp` 存一条远程记录，再 `POST /api/mcp/oauth/start`，把授权网址的参数拆开打印；最后一行是假授权服务器收到的每个请求。端口号换成了 `PORT`。和第一版的输出相比多了两样东西：`resource` 参数——第 21 章改写修好了假服务器的元数据文档，SDK 现在真的用上了它；`POST /protected` 那一个请求——§3.1。）

注意 `redirect_uri`：`http://console.local/api/mcp/oauth/callback`。
那个主机名不是配置出来的，是 `request.base_url` ——**谁应答了这次 start 请求，
授权服务器的重定向就得落回谁那里**。本地开发是 `localhost:8765`，部署到
真实域名就是那个域名，同一条存储记录不用改，也没有一个"公开 URL"设置项
等着被填错。

### 3.1 代码：`web/oauth.py`

先看文件开头。说明讲的就是上面 §2、§3 的内容；import 的那一长串是 SDK 的积木：

```python
"""Interactive OAuth for a browser that is not on the machine running the agent.

Chapter 21's OAuth is written for a CLI: `build_transport` hands
`OAuthClientProvider` a `redirect_handler` that prints a URL and opens a local
browser, and a `callback_handler` that opens a loopback socket on the same
machine and blocks until the one request it is waiting for arrives. Both
assume the process asking for authorization and the browser doing the
authorizing share a machine -- true for a terminal, false for this console:
the agent runs on a server, the browser is wherever the person reading this
is, and it can reach that server only over the port this console already
listens on (F22-01).

So the shape has to be two HTTP requests to *this console's own routes*
instead of one blocking call. `POST /api/mcp/oauth/start` (`routes.py`) does
everything `OAuthClientProvider` would do before it needs a person -- discover
the authorization server, register or seed a client, generate PKCE -- and
hands back a URL instead of opening one. `GET /api/mcp/oauth/callback` is
where the authorization server's redirect actually lands, arbitrarily later,
quite possibly on a different worker thread of this same process; it has
nothing to go on except the query string the browser carries back, and
`PendingOAuthTable` is what lets it find its way back to the attempt `start`
began (F22-02, F22-03).

That two-request split means the generator inside `OAuthClientProvider.
async_auth_flow` -- which only ever runs the whole way through in response to
one live 401 -- is not reusable here as-is: this module drives the same steps
directly instead, through the SDK's own (unexported but public, undocumented
only in the sense that it lives one module below the package's `__init__`)
building blocks in `mcp.client.auth.oauth2` and `mcp.client.auth.utils`:
`OAuthContext` for the request/response shapes and its already-correct
`prepare_token_auth`/`get_resource_url` methods, `PKCEParameters.generate()`
for the challenge, and the free functions for protected-resource discovery,
authorization-server discovery and dynamic registration. None of RFC 7591,
RFC 8414 or RFC 7636 is reimplemented; what is new here is only the seam that
splits one blocking call into two HTTP requests a browser can sit between.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin

import httpx2
from mcp.client.auth import OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from mcp.client.auth.oauth2 import OAuthContext, PKCEParameters, check_registration_usable
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_registration_request,
    create_oauth_metadata_request,
    extract_resource_metadata_from_www_auth,
    extract_scope_from_www_auth,
    get_client_metadata_scopes,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
    handle_token_response_scopes,
    validate_authorization_response_iss,
)
from mcp.shared.auth import OAuthClientMetadata

from minicodex.mcp_oauth import OwnerTokenStorage
from minicodex.remote import RemoteServerConfig
from minicodex.tenancy import DEFAULT_OWNER, Owner

# One round trip per discovery hop, at most a handful of hops (F21-08's own
# two-URL fallback for each of the two well-known documents). 30s matches
# `remote.py`'s own `_CONNECT_TIMEOUT` -- there is no reason a metadata fetch
# should be allowed to take longer than an actual MCP connection is.
_DISCOVERY_TIMEOUT = 30.0

# How long a `start` that nobody ever finished authorizing is allowed to sit in
# memory (F22-03). Twice chapter 21's loopback `CALLBACK_TIMEOUT_SECONDS`
# (300s): the loopback case times out the moment the *agent process* gives up
# waiting, one await; this one is a browser tab a person may have alt-tabbed
# away from, reached through a redirect rather than a direct socket, so it
# gets more slack rather than the same number reused without thinking about
# it.
DEFAULT_PENDING_TTL_SECONDS = 600.0
```

> - **`_DISCOVERY_TIMEOUT = 30.0`**：每一跳发现请求最多 30 秒，和第 21 章 `remote.py` 的连接超时一样。
> - **`DEFAULT_PENDING_TTL_SECONDS = 600.0`**：一次没完成的尝试在内存里最多留 10 分钟（§5）。
> - 第一版这里还有一个 `OAuthStartError`，说明是"`begin()` 拿不到可用的授权网址"——整个程序里没有一处抛它。改写时删了。

一次尝试要记住的东西，和存它们的那张表：

```python
@dataclass
class PendingOAuth:
    """One `start` call's worth of state, waiting for its `callback`.

    `context` carries everything `finish()` needs that `start()` already
    learned -- discovered endpoints, the scope actually granted, the client
    this attempt is registered (or preset) as -- so the callback never has to
    rediscover anything the browser round trip did not change.
    """

    state: str
    server_id: str
    server_name: str
    owner: Owner
    redirect_uri: str
    code_verifier: str
    context: OAuthContext
    created_at: float


class PendingOAuthTable:
    """Every OAuth attempt this process has started and not yet completed.

    In-process and in-memory, like `ApprovalBroker` (`approver.py`) and for
    the same reason: this console's whole state is file-or-memory (`store.py`
    's own docstring), and an attempt that outlives the process is one nobody
    can click "authorize" fast enough to save anyway. Expiry is swept lazily,
    on the next `put`/`pop`, rather than by a background task -- one fewer
    thing running when nothing is happening, at the cost of a table that can
    hold one extra expired entry between sweeps, which is a cost this project
    is willing to pay the same way `JobStore` (chapter 17) already does.
    """

    def __init__(self, ttl: float = DEFAULT_PENDING_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._table: dict[str, PendingOAuth] = {}

    def _sweep(self) -> None:
        now = time.time()
        expired = [state for state, p in self._table.items() if now - p.created_at > self._ttl]
        for state in expired:
            del self._table[state]

    def put(self, pending: PendingOAuth) -> None:
        self._sweep()
        self._table[pending.state] = pending

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

    def __len__(self) -> int:
        self._sweep()
        return len(self._table)
```

> - **`PendingOAuth`**：`state`（钥匙）、哪个服务器（id 和名字）、谁的（`owner`）、回调地址、PKCE 的 `code_verifier`（换 token 时要交出去的那个原串）、`context`（SDK 的 `OAuthContext`，装着发现出来的端点和注册好的客户端）、什么时候开始的。
> - **`PendingOAuthTable`**：一个字典，键是 `state`。`put` 存、`pop` 取出并删除、`__len__` 数一数——三个都先 `_sweep()`（§5）。`pop` 的说明是 §4 的内容。

然后是发现。这一段这次改写改过：

```python
# The request a connection starts with.  `_discover` sends it without a token
# for the one thing the answer carries: the server's own `WWW-Authenticate`.
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "minicodex-console", "version": "0"},
    },
}


async def _challenge(context: OAuthContext, client: httpx2.AsyncClient) -> httpx2.Response | None:
    """Ask the server, unauthenticated, what it wants -- the 401 the SDK's own
    flow reacts to.  `None` when it does not answer 401."""
    response = await client.post(
        context.server_url,
        json=_INITIALIZE,
        headers={"Accept": "application/json, text/event-stream"},
    )
    return response if response.status_code == 401 else None


async def _discover(context: OAuthContext, client: httpx2.AsyncClient) -> None:
    """Steps 1-2 of `OAuthClientProvider.async_auth_flow`'s 401 branch, run
    directly instead of waiting for a 401 to trigger them.

    Including the 401 itself.  The first version passed `None` where the SDK
    passes what the server's `WWW-Authenticate` names -- the protected-
    resource metadata URL and the scope -- and so only ever *guessed* the
    well-known paths on the MCP server's own origin.  RFC 9728 makes the
    header the primary way a server says where its metadata is; a server that
    says it only there connected from the CLI (the SDK reads the header) and
    not from the console.  Found when chapter 22 was rewritten, by the first
    test that put the MCP endpoint and its authorization server on different
    ports.

    Failure here is not fatal in itself -- a server with no protected-resource
    metadata and no authorization-server metadata is legal (SEP-985's fallback
    chain existing at all is the SDK's own admission of this), and the
    fallback endpoints computed from `config.url` alone are exactly what a
    server like that expects to be asked. What is fatal is downstream: no
    `authorization_endpoint` to send anyone to, or `check_registration_usable`
    refusing what got registered.
    """
    challenge = await _challenge(context, client)
    named = extract_resource_metadata_from_www_auth(challenge) if challenge else None
    for url in build_protected_resource_metadata_discovery_urls(named, context.server_url):
        response = await client.send(create_oauth_metadata_request(url))
        prm = await handle_protected_resource_response(response)
        if prm is not None:
            context.protected_resource_metadata = prm
            if prm.authorization_servers:
                context.auth_server_url = str(prm.authorization_servers[0])
            break

    for url in build_oauth_authorization_server_metadata_discovery_urls(
        context.auth_server_url, context.server_url
    ):
        response = await client.send(create_oauth_metadata_request(url))
        ok, asm = await handle_auth_metadata_response(response)
        if asm is not None:
            context.oauth_metadata = asm
            break
        if not ok:
            break

    context.client_metadata.scope = get_client_metadata_scopes(
        extract_scope_from_www_auth(challenge) if challenge else None,
        context.protected_resource_metadata,
        context.oauth_metadata,
        context.client_metadata.grant_types,
    )

```

> - **`_INITIALIZE`**：一个最普通的 MCP `initialize` 请求——一个连接开始时发的就是它。
> - **`_challenge`**：不带 token 把它发给 MCP 服务器。一个要认证的服务器会答 401，`WWW-Authenticate` 头里说明去哪认证。不是 401 就返回 `None`。
> - **`_discover`** 两步：
>   1. 受保护资源元数据（PRM）：`build_protected_resource_metadata_discovery_urls(named, server_url)` 给出要试的网址列表——如果 401 里**点名**了一个（`named`），它排第一；然后是 RFC 9728 的两个固定路径。试到第一个能用的为止，从里面取出授权服务器的地址。
>   2. 授权服务器元数据：同样一个列表，试到第一个能用的为止；遇到一个"明确失败"（`not ok`）就停。
>   3. 最后决定要哪些权限（`scope`）：优先 401 里说的，其次 PRM 里的，再次授权服务器的。
> - 这些 `build_...`、`handle_...` 都是 SDK 的函数；这里只是按 SDK 自己的 401 分支的顺序调用它们。

#### 改写时发现：控制台从来没问过服务器

第一版的 `_discover` 没有 `_challenge`，两处都传的是 `None`：

```python
    for url in build_protected_resource_metadata_discovery_urls(None, context.server_url):
    ...
    context.client_metadata.scope = get_client_metadata_scopes(
        None,
```

SDK 自己的流程（第 21 章，命令行走的那条）是**收到 401 之后**才开始发现的，第一个参数是从那个 401 的 `WWW-Authenticate` 里取出来的 `resource_metadata` 和 `scope`。说明文字也写着"这是 SDK 401 分支的第 1、2 步，直接跑"——可它跳过了 401 本身，于是只能**猜**：在 MCP 服务器**那台主机**上试两个固定路径。

RFC 9728 里，401 的头是服务器说明"去哪认证"的**主要**方式，固定路径是备用的。一个只在头里说明的服务器：命令行连得上（SDK 读了头），控制台连不上——授权网址会被拼到 MCP 服务器自己的主机上，那里根本没有 `/authorize`。

为什么测试没发现：第一版所有测试里，MCP 服务器和授权服务器是**同一个** `OAuthStub`——猜的那个主机恰好就是对的（第 21 章 §6.1 是同一个形状）。这次改写第一次把两者放到不同端口上（§9.1 的测试），这个测试先失败在这里：

```
>       raise KeyError(key)
E       KeyError: 'location'
```

（假浏览器去访问授权网址，拿到的是 MCP 假服务器的 404，没有跳转。）

修法就是上面的 `_challenge`：先问一次。测试——只测 `begin()`，一个只在 401 里说明元数据位置的 MCP 端点：

```python
async def test_begin_follows_the_metadata_url_the_server_names_in_its_401(
    tmp_path: Path,
) -> None:
    """The narrow version of the test above: `begin()` alone, against an MCP
    endpoint whose metadata is only discoverable from its challenge.  The
    first version guessed well-known paths on the endpoint's own origin and
    built an authorize URL there."""
    from mcp_http_stub import HttpMcpStub

    async with (
        OAuthStub(supports_dcr=False) as auth,
        HttpMcpStub(require_auth=True, challenge=auth.prm_url) as mcp,
    ):
        auth.resource = mcp.url
        config = RemoteServerConfig(
            name="gh", url=mcp.url, oauth=RemoteOAuthConfig(client_id="preset")
        )
        authorize_url, _ = await begin(
            config,
            server_id="srv-1",
            redirect_uri="http://console.test/api/mcp/oauth/callback",
            owner=Owner(home=tmp_path / "home"),
        )
    assert authorize_url.startswith(f"{auth.base}/authorize?")
```

> 断言授权网址落在 `OAuthStub` 的地址上，而不是 MCP 端点的。第一版在这里构造出的是 `http://127.0.0.1:<MCP 的端口>/authorize?...`。

### 3.2 代码：`begin()` 和 `finish()`

```python
async def begin(
    config: RemoteServerConfig,
    *,
    server_id: str,
    redirect_uri: str,
    owner: Owner = DEFAULT_OWNER,
) -> tuple[str, PendingOAuth]:
    """Discover, register (or confirm a preset client), generate PKCE, and
    build the URL a person needs to open. Returns `(authorize_url, pending)`.

    Raises `OAuthRegistrationError` when the server has no
    `registration_endpoint` and this server's record carries no preset
    `client_id` -- GitHub's own authorization server, measured in chapter 21
    (`probe_mcp_remote.py wellknown`), is exactly this case: it does not
    advertise dynamic client registration, so an interactive "Connect GitHub"
    click only reaches a URL when the console operator has already registered
    a GitHub OAuth App and put its `client_id` on this server's record.
    """
    assert config.oauth is not None
    storage = OwnerTokenStorage(owner, server=config.name)
    if config.oauth.client_id:
        await storage.seed_client_id(config.oauth.client_id, redirect_uri=redirect_uri)

    context = OAuthContext(
        server_url=config.url,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[redirect_uri],  # type: ignore[list-item]
            scope=config.oauth.scope,
        ),
        storage=storage,
        redirect_handler=None,
        callback_handler=None,
    )
    context.client_info = await storage.get_client_info()

    async with httpx2.AsyncClient(timeout=httpx2.Timeout(_DISCOVERY_TIMEOUT)) as client:
        await _discover(context, client)

        if context.client_info is None:
            registration_request = create_client_registration_request(
                context.oauth_metadata,
                context.client_metadata,
                context.get_authorization_base_url(config.url),
            )
            registration_response = await client.send(registration_request)
            client_info = await handle_registration_response(registration_response)
            check_registration_usable(client_info)
            context.client_info = client_info
            await storage.set_client_info(client_info)

    if context.oauth_metadata and context.oauth_metadata.authorization_endpoint:
        auth_endpoint = str(context.oauth_metadata.authorization_endpoint)
    else:
        auth_endpoint = urljoin(context.get_authorization_base_url(config.url) + "/", "authorize")

    pkce = PKCEParameters.generate()
    state = secrets.token_urlsafe(32)
    auth_params = {
        "response_type": "code",
        "client_id": context.client_info.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
    }
    if context.should_include_resource_param(None):
        auth_params["resource"] = context.get_resource_url()
    if context.client_metadata.scope:
        auth_params["scope"] = context.client_metadata.scope

    authorize_url = f"{auth_endpoint}?{urlencode(auth_params)}"
    pending = PendingOAuth(
        state=state,
        server_id=server_id,
        server_name=config.name,
        owner=owner,
        redirect_uri=redirect_uri,
        code_verifier=pkce.code_verifier,
        context=context,
        created_at=time.time(),
    )
    return authorize_url, pending
```

> - **预置的 `client_id`**：先写进存储（和第 21 章 `build_oauth_provider` 一样，第 21 章 §7 讲的 codex 的姿态）。
> - **`OAuthContext(...)`**：SDK 用来装"这一次授权流程知道的一切"的对象。两个 handler 传 `None`——这里根本不会走到需要它们的地方。
> - **没有客户端信息**：去注册（RFC 7591）。没有 `registration_endpoint` 的服务器（比如 GitHub 的）会在这里抛 `OAuthRegistrationError`——说明里讲了，这就是为什么连 GitHub 要先手工注册一个 OAuth App（§11）。
> - **授权端点**：元数据里有就用，没有就用"授权服务器地址 + `/authorize`"。
> - **PKCE 和 `state`**：`PKCEParameters.generate()` 生成 verifier 和 challenge；`state` 是 `secrets.token_urlsafe(32)`——服务端生成，§4 讲为什么。
> - **拼网址**：`urlencode` 把参数字典变成 `a=1&b=2`。需要的时候加上 `resource` 和 `scope`。
> - **返回** `(授权网址, PendingOAuth)`——网址交给浏览器，`PendingOAuth` 交给表。**这个函数不打开任何东西。**

```python
async def finish(pending: PendingOAuth, *, code: str, iss: str | None) -> None:
    """The callback's half: exchange the code, persist the tokens.

    Everything needed to build the token request was already learned in
    `begin()` and carried on `pending.context` -- this function makes exactly
    one more network call, to the token endpoint, and one disk write, to
    `OwnerTokenStorage`.
    """
    context = pending.context
    validate_authorization_response_iss(iss, context.oauth_metadata)
    assert context.client_info is not None

    if context.oauth_metadata and context.oauth_metadata.token_endpoint:
        token_url = str(context.oauth_metadata.token_endpoint)
    else:
        token_url = urljoin(context.get_authorization_base_url(context.server_url) + "/", "token")

    token_data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": context.client_info.client_id,
        "code_verifier": pending.code_verifier,
    }
    if context.should_include_resource_param(None):
        token_data["resource"] = context.get_resource_url()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_data, headers = context.prepare_token_auth(token_data, headers)

    async with httpx2.AsyncClient(timeout=httpx2.Timeout(_DISCOVERY_TIMEOUT)) as client:
        response = await client.post(token_url, data=token_data, headers=headers)
    if response.status_code not in (200, 201):
        body = (await response.aread()).decode("utf-8", errors="replace")
        raise OAuthTokenError(f"token exchange failed ({response.status_code}): {body}")
    token = await handle_token_response_scopes(response)
    if token.scope is None:
        token.scope = context.client_metadata.scope
    await context.storage.set_tokens(token)
```

> - 回调的那一半：验证 `iss`（防止换了授权服务器的混淆攻击，SDK 的函数），拼 token 请求——`code`、回调地址、`client_id`、**`code_verifier`**——发出去，存 token。
> - `prepare_token_auth`：SDK 的方法，按客户端的认证方式（有没有 `client_secret`）调整请求。
> - 非 200/201：把响应体放进 `OAuthTokenError` 的消息里。这个消息会出现在回调页面上——§4.1 的事。

### 3.3 代码：`start` 路由

`routes.py` 里，这一章的部分从一段注释开始：

```python
# -- MCP OAuth: the console's own architecture, not chapter 21's -------------
#
# See `web/oauth.py`'s module docstring for why chapter 21's `redirect_handler`
# /`callback_handler` do not carry over (F22-01). The short version lives here
# too: those two assume the agent process and the browser share a machine, and
# a server-hosted console cannot assume that, so the callback has to land on
# one of *this console's own* routes instead of a loopback port, correlated to
# a pending attempt by a server-generated `state` (F22-02) that expires if
# nobody ever finishes authorizing (F22-03).
```

```python
def _mcp_record(console: Any, server_id: str) -> dict[str, Any]:
    record = console.store.get("mcp.json", server_id)
    if record is None:
        raise HTTPException(404, "no such server")
    if record_kind(record) != "remote":
        raise HTTPException(400, f"{record['name']} is a local command, not a remote server")
    return record


class OAuthStartIn(BaseModel):
    server_id: str


def _token_fingerprint(tokens: Any) -> str | None:
    """Sixteen hex characters that change whenever the stored token does --
    and that say nothing about the token itself."""
    if tokens is None:
        return None
    return hashlib.sha256(tokens.access_token.encode("utf-8")).hexdigest()[:16]


@router.post("/api/mcp/oauth/start")
async def start_mcp_oauth(request: Request, body: OAuthStartIn) -> dict[str, Any]:
    console = _console(request)
    record = _mcp_record(console, body.server_id)
    try:
        config = server_config(record)
    except RemoteConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    if config.oauth is None:
        raise HTTPException(
            400, f"{record['name']} is not set up for OAuth -- enable it when adding the server"
        )
    # Reflects the Host the browser is actually using rather than a
    # configured "public URL" this console does not otherwise need to know,
    # so the same record works unchanged in local dev and behind a reachable
    # host -- whatever answered this request is where the authorization
    # server's redirect has to land too.
    redirect_uri = str(request.base_url) + "api/mcp/oauth/callback"
    try:
        authorize_url, pending = await mcp_oauth_flow.begin(
            config, server_id=record["id"], redirect_uri=redirect_uri, owner=DEFAULT_OWNER
        )
    except (
        mcp_oauth_flow.OAuthFlowError,
        mcp_oauth_flow.OAuthRegistrationError,
        mcp_oauth_flow.OAuthTokenError,
        OAuthStorageError,
        httpx2.HTTPError,
    ) as exc:
        raise HTTPException(400, f"could not start OAuth for {record['name']}: {exc}") from exc
    console.mcp_oauth.put(pending)
    # `previous`: which token is on file *before* this attempt, so the browser
    # can wait for a different one (`mcpOauth.ts`).  See `mcp_oauth_status`.
    previous = await OwnerTokenStorage(DEFAULT_OWNER, server=record["name"]).get_tokens()
    return {"authorize_url": authorize_url, "previous": _token_fingerprint(previous)}
```

> - **`_mcp_record`**：按 id 找记录；没有就 404，是本地命令就 400。
> - **`start_mcp_oauth`**：把记录变成 config（§6.2 的 `server_config`）；没开 OAuth 就 400；`redirect_uri` 用 `request.base_url` 拼——上面 §3 讲的那件事；调 `begin()`，它的几种失败都变成 400 加一句人话；把 pending 放进表；返回授权网址。
> - **`previous`** 和 `_token_fingerprint` 是这次改写加的，§8.1 讲。`OAuthStorageError` 那一项也是（第 21 章改写让坏掉的 token 文件变成这个异常）。

---

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


回调路由本身：

```python
@router.get("/api/mcp/oauth/callback")
async def mcp_oauth_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    iss: str | None = None,
) -> HTMLResponse:
    console = _console(request)
    # `state` is looked up, never trusted (F22-02): an unknown, expired or
    # missing value gets the same answer as a value that was never issued at
    # all, and nothing past this line reads anything else the query string
    # says about *which* server this is.
    pending = console.mcp_oauth.pop(state) if state else None
    if pending is None:
        return _oauth_result_page(
            ok=False, message="this authorization link is unknown or has expired"
        )
    if error is not None:
        reason = error_description or error
        return _oauth_result_page(
            ok=False, message=f"{pending.server_name}: authorization was not granted ({reason})"
        )
    if not code:
        return _oauth_result_page(
            ok=False, message=f"{pending.server_name}: no authorization code was returned"
        )
    try:
        await mcp_oauth_flow.finish(pending, code=code, iss=iss)
    except (
        mcp_oauth_flow.OAuthFlowError,
        mcp_oauth_flow.OAuthTokenError,
        OAuthStorageError,
        httpx2.HTTPError,
    ) as exc:
        return _oauth_result_page(ok=False, message=f"{pending.server_name}: {exc}")
    return _oauth_result_page(ok=True, message=f"connected to {pending.server_name}")
```

> - 五个查询参数，都可以没有（`str | None = None`）。
> - **第一件事是 `pop(state)`**：没有 `state`、查不到——同一句"unknown or has expired"，不往下走。
> - 授权服务器说"不同意"（`error`）、没给 `code`：各一句话。
> - 否则 `finish()`，失败变成一个"连接失败"的页面，成功是"connected to ..."。
> - **注意哪里用了 `pending.server_name`**：服务器的名字来自 pending 条目（服务端自己写的），不是查询字符串。

测试直接把这条钉死。先是表本身：

```python
def test_F22_02_an_unissued_state_returns_nothing() -> None:
    table = PendingOAuthTable()
    assert table.pop("nobody-ever-issued-this") is None


def test_F22_02_a_state_is_good_for_exactly_one_callback(tmp_path: Path) -> None:
    table = PendingOAuthTable()
    pending = _dummy_pending("state-1", created_at=time.time(), home=tmp_path)
    table.put(pending)
    assert table.pop("state-1") is pending
    assert table.pop("state-1") is None, "a state must not be usable a second time"


async def test_F22_02_two_attempts_never_share_a_state(tmp_path: Path) -> None:
    """A guessable or reused `state` is an open redirect/CSRF surface (the
    fault this ID is named for) -- `begin()` has to draw a fresh one every
    time, not just look one up."""
    owner = Owner(home=tmp_path / "home")
    async with OAuthStub(supports_dcr=False) as auth:
        config = RemoteServerConfig(
            name="gh", url=auth.resource_url, oauth=RemoteOAuthConfig(client_id="preset-client")
        )
        _url_a, pending_a = await begin(
            config, server_id="a", redirect_uri="https://console.example/cb", owner=owner
        )
        _url_b, pending_b = await begin(
            config, server_id="b", redirect_uri="https://console.example/cb", owner=owner
        )
    assert pending_a.state != pending_b.state
```

> - `_dummy_pending`（测试文件开头的帮手，§13 引全文）造一个不需要网络的 `PendingOAuth`。
> - 第三个：真的调两次 `begin()`，两个 `state` 不同。

然后是路由：

```python
def test_F22_02_the_callback_route_answers_an_unknown_state_without_touching_storage(
    console: TestClient,
) -> None:
    response = console.get(
        "/api/mcp/oauth/callback", params={"state": "not-real", "code": "whatever"}
    )
    assert response.status_code == 200
    assert "unknown or has expired" in response.text


def test_F22_02_a_missing_state_is_treated_the_same_as_an_unknown_one(
    console: TestClient,
) -> None:
    response = console.get("/api/mcp/oauth/callback", params={"code": "whatever"})
    assert response.status_code == 200
    assert "unknown or has expired" in response.text
```

> 第一个的名字里有"without touching storage"，测试里没有检查存储——它只断言了那句话。名字说得比测的多，记在这里。

最要紧的一条，"同一个链接用两次"，是一个端到端测试：真的走一遍 `start` → 假授权服务器 → `callback`，再用同一个链接回调一次：

```python
async def test_a_second_use_of_the_same_callback_link_is_refused(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state was popped the first time through; a reload of the same
    callback URL (a person double-clicking a stale link, a browser replaying
    a request) must not repeat the token exchange."""
    async with (
        OAuthStub(supports_dcr=False) as auth,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        created = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": auth.resource_url,
                    "oauth": True,
                    "oauth_client_id": "preset-client",
                },
            )
        ).json()
        authorize_url = (
            await api.post("/api/mcp/oauth/start", json={"server_id": created["id"]})
        ).json()["authorize_url"]
        async with httpx2.AsyncClient() as browser:
            response = await browser.get(authorize_url, follow_redirects=False)
        params = parse_qs(urlparse(response.headers["location"]).query)
        query = {"code": params["code"][0], "state": params["state"][0]}

        first = await api.get("/api/mcp/oauth/callback", params=query)
        assert "connected to gh" in first.text
        assert auth.token_calls == 1

        second = await api.get("/api/mcp/oauth/callback", params=query)
        assert "unknown or has expired" in second.text
        assert auth.token_calls == 1, "the code must not be exchanged twice"
```

一个人双击了一个过期链接、一个浏览器重放了请求——`token_calls` 都还是 1。

### 4.1 改写时发现：回调页面会执行授权服务器写的 HTML

结果页面是一个字符串模板（第一版）：

```python
def _oauth_result_page(*, ok: bool, message: str) -> HTMLResponse:
    title = "Connected" if ok else "Connection failed"
    return HTMLResponse(_OAUTH_RESULT_PAGE.format(title=title, message=message))
```

`message` 里有什么？看回调路由：

- `reason = error_description or error` ——**授权服务器重定向时带回来的**；
- `f"{pending.server_name}: {exc}"` ——`exc` 可能是 `OAuthTokenError`，它的消息里有**token 端点的响应体原文**（§3.2 `finish`）。

两样都是授权服务器写的。而授权服务器是谁的？是**那个远程 MCP 服务器的运营者**的——第 21 章 §11 的"陌生人"。一个恶意的（或被攻破的）授权服务器在重定向里带上这么一句：

```
$ uv run python xss22.py
200 text/html; charset=utf-8
<p>gh: authorization was not granted (<img src=x onerror="fetch('/api/mcp',{method:'POST'})">)</p><p>You can close this tab.</p></body></htm
```

（`xss22.py`：往控制台的待完成表里放一条 `state="s1"` 的记录，然后用 `error=access_denied&error_description=<img ...>` 访问回调。）

那个 `<img>` 原样进了页面。页面在**控制台自己的网址**上——和 `/api/...` 同源，浏览器会让它调用控制台的每一个接口。第 22 章的控制台没有登录（那是第 23 章），所以一段 `fetch('/api/mcp', {method: 'POST', body: ...})` 就能添加一个 **stdio 服务器，命令随便写**——下一轮对话开始时，它在服务器上被执行。

要做到这一步，攻击者只需要让用户点一次"Connect"，或者知道一个还没过期的 `state`（它就在授权网址里，授权服务器当然知道）。

修法：一行，`html.escape`。

```python
_OAUTH_RESULT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font: 15px/1.6 ui-sans-serif, system-ui, sans-serif; max-width: 32rem;
         margin: 16vh auto; padding: 0 1.5rem; color: #10182B; background: #EDF1FA;
         text-align: center; }}
  h1 {{ font-size: 1.25rem; }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #EAF0FF; background: #0A1120; }} }}
</style></head>
<body><h1>{title}</h1><p>{message}</p><p>You can close this tab.</p></body></html>"""


def _oauth_result_page(*, ok: bool, message: str) -> HTMLResponse:
    """The page the authorization server's redirect lands on.

    `message` is escaped.  The first version formatted it in raw, and it
    carries text this console did not write: `error_description` straight
    from the redirect, and the token endpoint's own response body when the
    exchange fails -- both chosen by whoever runs the remote server's
    authorization server.  Measured when chapter 22 was rewritten: an
    `<img onerror=...>` in `error_description` came back as live HTML, on the
    console's own origin, where a script can call `POST /api/mcp` and add a
    stdio server with any command it likes.
    """
    title = "Connected" if ok else "Connection failed"
    return HTMLResponse(_OAUTH_RESULT_PAGE.format(title=title, message=html.escape(message)))
```

> `html.escape` 把 `<` 变成 `&lt;`、`"` 变成 `&quot;`……浏览器把它们显示成文字。`title` 只有两个固定值，不需要转义。

```python
def test_the_callback_page_escapes_what_the_authorization_server_sent(
    console: TestClient, tmp_path: Path
) -> None:
    """`error_description` comes from the redirect -- from whoever runs the
    authorization server.  The first version put it into the page as HTML,
    on the console's own origin."""
    app_console = console.app.state.console  # type: ignore[attr-defined]
    app_console.mcp_oauth.put(_dummy_pending("s-xss", created_at=time.time(), home=tmp_path))
    page = console.get(
        "/api/mcp/oauth/callback",
        params={
            "state": "s-xss",
            "error": "access_denied",
            "error_description": '<img src=x onerror="alert(1)">',
        },
    ).text
    assert "<img" not in page
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page
```

> - `console.app.state.console`：测试直接拿到控制台对象，往它的待完成表里放一条记录（`console` 是 `TestClient`，`.app` 是它包着的应用）。
> - 断言页面里没有 `<img`，有转义后的 `&lt;img src=x onerror=&quot;alert(1)&quot;&gt;`。

`minicodex-live` 里的这个函数本来就有 `html.escape`——交付版在别处修过。第 22、23 步没有，这次补上了。

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

> 先把过期的 `state` 收集到一个列表里，再逐个删——一边遍历字典一边删除它会出错。

代价是两次清扫之间可能多留一条过期条目。这个取舍第 17 章的 `JobStore` 已经
做过一次，理由一样：**没事发生的时候，少一个东西在跑。**

十分钟这个数字是按"一个人读完一屏真实的授权同意页"定的，不是按机器定的。

测试：

```python
def test_F22_03_a_pending_attempt_expires(tmp_path: Path) -> None:
    table = PendingOAuthTable(ttl=1.0)
    pending = _dummy_pending("expired", created_at=time.time() - 2.0, home=tmp_path)
    table.put(pending)
    assert table.pop("expired") is None


def test_F22_03_a_fresh_attempt_survives_the_sweep(tmp_path: Path) -> None:
    table = PendingOAuthTable(ttl=600.0)
    pending = _dummy_pending("fresh", created_at=time.time(), home=tmp_path)
    table.put(pending)
    assert table.pop("fresh") is pending


def test_F22_03_expiry_is_swept_lazily_rather_than_by_a_background_task(tmp_path: Path) -> None:
    """`put`/`pop` are the only two places `_sweep` runs from -- an attempt
    that expired between two calls disappears the moment either is called
    again, with nothing running in the background to notice sooner."""
    table = PendingOAuthTable(ttl=1.0)
    table.put(_dummy_pending("a", created_at=time.time() - 5.0, home=tmp_path))
    table.put(_dummy_pending("b", created_at=time.time(), home=tmp_path))
    assert len(table) == 1  # "a" was already stale when "b" triggered the sweep
```

> - 第一个：TTL 1 秒，`created_at` 设在 2 秒以前，`pop` 拿不到。
> - 第二个：刚刚创建的，扫过以后还在（§17 练习 1 讲它为什么更容易写错）。
> - 第三个：先放一条早就过期的 `a`，再放一条新的 `b`——放 `b` 的那次 `put` 顺手扫掉了 `a`，所以 `len` 是 1。它证明的是"`put` 会扫"；"没有后台任务"这一半，测试没法直接证明一件事**不**存在，靠的是读代码。

---

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

`add_mcp` 收的表单，一个模型装两种形状：

```python
# Two shapes behind one record, the same split `mcp.load_config` already reads
# from a config file (F21-01's dual validation, done again here because a
# library rejecting a bad shape raises for the caller and a web form rejecting
# one has to become a sentence a person reads -- F22-04): a stdio server has a
# `command`; a remote one has a `url` and one of two authentication paths,
# a bearer token or OAuth, never both.


class McpIn(BaseModel):
    name: str
    # stdio
    command: str | None = None  # shell-style; split with shlex, see below
    env: dict[str, str] = {}
    cwd: str | None = None
    # remote
    url: str | None = None
    bearer_token: str | None = None
    http_headers: dict[str, str] = {}
    env_http_headers: dict[str, str] = {}
    oauth: bool = False
    oauth_client_id: str | None = None
    oauth_scope: str | None = None
    # both
    startup_timeout: float = 30.0
    tool_timeout: float = 60.0
```

> 所有字段都有默认值：stdio 只填 `command`/`env`/`cwd`，远程只填 `url` 和认证那几项。

```python
@router.post("/api/mcp")
def add_mcp(request: Request, body: McpIn) -> dict[str, Any]:
    has_command = bool(body.command and body.command.strip())
    has_url = bool(body.url and body.url.strip())
    if has_command and has_url:
        raise HTTPException(
            400, "a server is reached one way or the other -- set 'command' or 'url', not both"
        )
    if not has_command and not has_url:
        raise HTTPException(400, "give this server either a command (stdio) or a url (remote)")
    # One record per name.  A remote server's OAuth token is stored under its
    # *name* (`OwnerTokenStorage(owner, server=name)`, chapter 21), so two
    # records called "gh" shared one token, and a token issued for one URL was
    # sent to the other.  Found when chapter 22 was rewritten; the tool names
    # two same-named servers produce collide anyway (chapter 9).
    if any(r.get("name") == body.name for r in _console(request).store.all("mcp.json")):
        raise HTTPException(409, f"a server called {body.name!r} already exists")
    if body.startup_timeout <= 0 or body.tool_timeout <= 0:
        raise HTTPException(400, "timeouts must be positive")

    if has_command:
        assert body.command is not None
        command = shlex.split(body.command)
        if not command:
            raise HTTPException(400, "command cannot be empty")
        record = {
            "kind": "stdio",
            "name": body.name,
            "command": command,
            "env": body.env,
            "cwd": body.cwd,
            "startup_timeout": body.startup_timeout,
            "tool_timeout": body.tool_timeout,
            "enabled": True,
        }
    else:
        if body.bearer_token and body.oauth:
            raise HTTPException(
                400,
                "a server is authenticated one way or the other -- "
                "a bearer token or OAuth, not both",
            )
        record = {
            "kind": "remote",
            "name": body.name,
            "url": body.url,
            "bearer_token": body.bearer_token or None,
            "http_headers": body.http_headers,
            "env_http_headers": body.env_http_headers,
            "oauth": (
                {"client_id": body.oauth_client_id or None, "scope": body.oauth_scope or None}
                if body.oauth
                else None
            ),
            "startup_timeout": body.startup_timeout,
            "tool_timeout": body.tool_timeout,
            "enabled": True,
        }
    return _public_mcp(_console(request).store.add("mcp.json", record))
```

> - 先判断两种形状：两个都有、两个都没有，都是 400（第 21 章 `load_config` 的同一条规则，这里换成人话）。
> - 名字已经被用了：409——这是这次改写加的，§6.1。
> - stdio：`shlex.split` 把一行命令拆成列表，存一条 `"kind": "stdio"` 的记录。
> - 远程：token 和 OAuth 只能二选一；存一条 `"kind": "remote"` 的记录，OAuth 打开时存一个 `{"client_id", "scope"}`。
> - 返回前经过 `_public_mcp`（§7）。

测试：

```python
def test_F22_04_add_mcp_refuses_a_command_and_a_url_together(console: TestClient) -> None:
    response = console.post(
        "/api/mcp", json={"name": "x", "command": "true", "url": "https://x.test/mcp"}
    )
    assert response.status_code == 400
    assert "not both" in response.json()["detail"]


def test_F22_04_add_mcp_refuses_neither_a_command_nor_a_url(console: TestClient) -> None:
    response = console.post("/api/mcp", json={"name": "x"})
    assert response.status_code == 400


def test_F22_04_add_mcp_refuses_a_bearer_token_and_oauth_together(console: TestClient) -> None:
    response = console.post(
        "/api/mcp",
        json={"name": "x", "url": "https://x.test/mcp", "bearer_token": "t", "oauth": True},
    )
    assert response.status_code == 400
    assert "not both" in response.json()["detail"]


def test_F22_04_server_config_refuses_the_same_combination_a_second_time(tmp_path: Path) -> None:
    """The same rule enforced again where a record actually turns into
    something that connects, in case one ever reaches here some other way
    than `add_mcp` -- a hand-edited store file, for instance."""
    with pytest.raises(RemoteConfigError):
        server_config(
            {
                "name": "x",
                "kind": "remote",
                "url": "https://x.test/mcp",
                "bearer_token": "t",
                "oauth": {"client_id": "cid"},
            }
        )
```

> 最后一个直接调 `server_config`（下面），给它一条手写的"token 加 OAuth"记录，断言抛 `RemoteConfigError`——上面说的"第二遍"。

### 6.1 改写时发现：两个服务器可以同名

`add_mcp`（第一版）不检查名字。可远程服务器的 OAuth token 是按**名字**存的（第 21 章：`OwnerTokenStorage(owner, server=config.name)`）——两条都叫 `gh` 的记录，一个指向 GitHub，一个指向别处，共用同一个 token：用 GitHub 的那个连好以后，另一个的请求也带着它出门。第 9 章的工具命名（`mcp__<服务器名>__<工具名>`）在同名时也会撞。

修法：名字已存在就 409。

```python
def test_two_servers_cannot_share_a_name(console: TestClient) -> None:
    """A remote server's token is stored under its name; two records with one
    name shared one token."""
    first = console.post("/api/mcp", json={"name": "gh", "url": "https://a.test/mcp"})
    assert first.status_code == 200
    second = console.post("/api/mcp", json={"name": "gh", "url": "https://b.test/mcp"})
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]
```

### 6.2 代码：`mcp_config.py`——一条记录变成一个 config

```python
"""Turning one stored `mcp.json` record into the config object `minicodex.mcp`
and `minicodex.remote` already understand.

One function, called from two places -- `routes.py`, to build a
`RemoteServerConfig` for `POST /api/mcp/oauth/start`, and `runtime.py`, to
build the list `connect()` takes for a turn. It lives here rather than in
either of them because it is the same translation both times, and a
translation written twice is a translation that drifts: chapter 3 made this
exact repair once already, for the tool descriptions duplicated between the
prompt and the schema.

Which shape a record is (`"stdio"` or `"remote"`) is **derived**, the same
rule `mcp.load_config` already applies to a config file's own `command`/`url`
split -- a record written before this chapter carried no `"kind"` field at
all, and there is no reason to make every one of them re-save itself just to
gain one.
"""

from __future__ import annotations

from typing import Any

from minicodex.mcp import AnyServerConfig, ServerConfig
from minicodex.remote import RemoteConfigError, RemoteOAuthConfig, RemoteServerConfig

DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_TOOL_TIMEOUT = 60.0


def record_kind(record: dict[str, Any]) -> str:
    """`"stdio"` or `"remote"` for one persisted `mcp.json` record."""
    kind = record.get("kind")
    if kind in ("stdio", "remote"):
        return kind
    return "remote" if record.get("url") else "stdio"


def server_config(record: dict[str, Any]) -> AnyServerConfig:
    """One stored record -> the config `mcp.connect` actually takes.

    The raw `bearer_token` a browser typed into the add-server form is folded
    into a static `Authorization` header here, not passed through
    `RemoteServerConfig.bearer_token_env_var` -- that field exists so a config
    *file* never has to hold a secret in the clear (chapter 21's reasoning,
    still true for `mcp.json` the CLI loads). The console's own `mcp.json` is
    not that file: it is local runtime state a browser wrote to, exactly like
    `providers.json`'s `api_key` (chapter 19) already is, and it is kept the
    same way for the same reason -- there is nowhere else for it to live.
    """
    startup = float(record.get("startup_timeout") or DEFAULT_STARTUP_TIMEOUT)
    tool = float(record.get("tool_timeout") or DEFAULT_TOOL_TIMEOUT)
    if record_kind(record) == "stdio":
        return ServerConfig(
            name=record["name"],
            command=tuple(record["command"]),
            env=dict(record.get("env") or {}),
            cwd=record.get("cwd"),
            startup_timeout=startup,
            tool_timeout=tool,
        )

    headers = dict(record.get("http_headers") or {})
    token = record.get("bearer_token")
    oauth_spec = record.get("oauth")
    if token and oauth_spec:
        # `add_mcp` already refuses this combination at the door (F22-04's
        # sibling check); this is the same rule enforced again where the
        # record is turned into something that actually connects, in case a
        # record ever reaches here some other way -- a hand-edited store file,
        # for instance.
        raise RemoteConfigError(
            f"{record.get('name', '?')}: a server is authenticated one way or "
            "the other -- a bearer token and OAuth cannot both be configured"
        )
    if token:
        headers.setdefault("Authorization", f"Bearer {token}")
    oauth = (
        RemoteOAuthConfig(client_id=oauth_spec.get("client_id"), scope=oauth_spec.get("scope"))
        if oauth_spec
        else None
    )
    return RemoteServerConfig(
        name=record["name"],
        url=record["url"],
        bearer_token_env_var=record.get("bearer_token_env_var"),
        http_headers=headers,
        env_http_headers=dict(record.get("env_http_headers") or {}),
        oauth=oauth,
        startup_timeout=startup,
        tool_timeout=tool,
    )


__all__ = ["DEFAULT_STARTUP_TIMEOUT", "DEFAULT_TOOL_TIMEOUT", "record_kind", "server_config"]
```

> - **说明**：为什么是单独一个文件——`routes.py`（`start` 要一个 `RemoteServerConfig`）和 `runtime.py`（一轮对话要 `connect()` 的列表）做的是**同一个翻译**，写两遍就会漂移。
> - **`record_kind`**：有 `kind` 就用；没有（第 22 章之前存的记录）就看有没有 `url`——"推出来，不要求声明"，和第 21 章 `load_config` 同一条规则。
> - **`server_config`**：stdio 记录变成 `ServerConfig`；远程记录：粘贴的 token 折成 `Authorization` 头（说明讲了为什么不走 `bearer_token_env_var`）；token 和 OAuth 同时存在就抛 `RemoteConfigError`（"第二遍"）；OAuth 那一节变成 `RemoteOAuthConfig`。

测试：

```python
def test_record_kind_is_derived_for_a_record_written_before_chapter_22() -> None:
    assert record_kind({"command": ["true"]}) == "stdio"
    assert record_kind({"url": "https://x.test/mcp"}) == "remote"


def test_server_config_folds_a_raw_bearer_token_into_an_authorization_header() -> None:
    config = server_config(
        {"name": "gh", "kind": "remote", "url": "https://x.test/mcp", "bearer_token": "tok"}
    )
    assert isinstance(config, RemoteServerConfig)
    assert config.http_headers["Authorization"] == "Bearer tok"


def test_server_config_builds_a_stdio_config_for_a_record_with_no_kind_key() -> None:
    config = server_config({"name": "notes", "command": ["python", "-m", "server"]})
    assert config.command == ("python", "-m", "server")
```

#### 改写时发现："第二遍"抛出来，整轮对话就没了

`server_config` 的注释说，那个"token 加 OAuth"的第二遍检查是为了防"一条通过别的路径进到存储里的记录，比如有人手工编辑了 `mcp.json`"。检查的方式是**抛异常**。它在哪儿被调用？`runtime.resolve_mcp_configs`，每一轮对话开始时，对每一条启用的记录：

```python
    for s in enabled:
        config = server_config(s)
```

没有 `try`。异常一路抛出 `run_turn`——这一轮失败，**连带所有其他服务器**，而且下一轮、再下一轮都一样，直到有人找到那个文件。一条 `url` 缺失的远程记录（`KeyError`）也是同样的结果。

防一条坏记录的检查，把它变成了一条让所有记录都用不了的记录。修法：这一条报告、跳过，其余照常：

```python
async def resolve_mcp_configs(enabled: list[dict[str, Any]], emit: EventSink) -> list[Any]:
    """Every enabled server record, minus the ones this turn cannot connect.

    A remote server with OAuth configured but no token on file yet is the one
    case this turn refuses outright rather than attempting and failing
    (F22-01): the only `redirect_handler`/`callback_handler` a turn could hand
    `connect()` are `_oauth_redirect_refused`/`_oauth_callback_refused` above,
    which exist to raise rather than to open a browser this process has no
    way to show anyone. Reported once, by name, as a status the console's MCP
    tab already renders -- "needs connection" -- rather than a stack trace
    that says nothing about what to click.

    Split out of `run_turn` so it can be tested on its own: what it decides
    does not depend on a model, an approval broker or a workspace, only on the
    stored records and whether `OwnerTokenStorage` already has a token.
    """
    configs = []
    for s in enabled:
        try:
            config = server_config(s)
        except (RemoteConfigError, KeyError, TypeError, ValueError) as exc:
            # One record that cannot become a config -- a hand-edited
            # `mcp.json` with a token *and* OAuth, a remote record with no
            # `url` -- is that record's problem, reported by its name.  The
            # first version let the exception out of here, and every turn
            # failed, with every server, until someone found the file.  Found
            # when chapter 22 was rewritten.
            emit(
                {
                    "type": "mcp_status",
                    "name": str(s.get("name", "?")),
                    "status": "error",
                    "detail": f"this server's saved settings cannot be used: {exc}",
                }
            )
            continue
        if record_kind(s) == "remote" and config.oauth is not None:
            storage = OwnerTokenStorage(DEFAULT_OWNER, server=config.name)
            if await storage.get_tokens() is None:
                emit(
                    {
                        "type": "mcp_status",
                        "name": config.name,
                        "status": "needs_connection",
                        "detail": "not connected yet -- use Connect in the MCP tab",
                    }
                )
                continue
        configs.append(config)
    return configs
```

> - `try` 包住 `server_config(s)`：`RemoteConfigError`（形状不对）、`KeyError`（缺字段）、`TypeError`/`ValueError`（字段类型或数值不对，比如 `float("abc")`）——发一条这个服务器名下的 `mcp_status` 错误事件，`continue`。
> - 下面那一段（OAuth 但还没 token）是 §9 的内容。

```python
async def test_one_unusable_record_is_reported_and_skipped_not_fatal_to_the_turn(
    owner: Owner,
) -> None:
    """A record that cannot become a config -- here a hand-edited one with a
    token *and* OAuth, the case `server_config` refuses a second time -- is
    reported under its own name, and the other servers still connect.  The
    first version raised out of `resolve_mcp_configs`, and every turn failed."""
    records = [
        {
            "name": "broken",
            "kind": "remote",
            "url": "https://x.test/mcp",
            "bearer_token": "t",
            "oauth": {"client_id": "cid"},
        },
        {"name": "files", "command": ["true"]},
    ]
    events: list[dict[str, Any]] = []
    configs = await web_runtime.resolve_mcp_configs(records, events.append)
    assert [c.name for c in configs] == ["files"]
    assert events[0]["name"] == "broken"
    assert events[0]["status"] == "error"
    assert "cannot be used" in events[0]["detail"]
```

## §7 token 存哪，以及一件要说实话的事

一个人在表单里粘进来的 bearer token，不走 `bearer_token_env_var`，直接折成一个
`Authorization` 头。

第 21 章设计那个字段是为了让配置**文件**不必明文存密钥——那个理由对 CLI 读的
`mcp.json` 现在也还成立。但控制台的存储不是那个文件：它是一个浏览器写出来的
本地运行状态，和第 19 章起 `providers.json` 里的 `api_key` 完全一样，就按同样的
方式对待。


```python
def _public_mcp(record: dict[str, Any]) -> dict[str, Any]:
    """A bearer token never leaves the process, not even to the browser that
    typed it in -- `_public` (below, for providers) drew this line first.

    `kind` is filled in here as well.  `types.ts` tells the frontend it can
    rely on `kind` "because the backend has already normalised" it -- and the
    first version did not: a stdio record saved before chapter 22 went out
    with no `kind`, and its card showed `m.url` (nothing) where the command
    should have been.  Found when chapter 22 was rewritten.
    """
    public = {**record, "kind": record_kind(record)}
    if record.get("bearer_token"):
        public["bearer_token"] = True
    return public


@router.get("/api/mcp")
def list_mcp(request: Request) -> list[dict[str, Any]]:
    return [_public_mcp(r) for r in _console(request).store.all("mcp.json")]


@router.post("/api/mcp/{server_id}/toggle")
def toggle_mcp(request: Request, server_id: str, body: McpToggle) -> dict[str, Any]:
    record = _console(request).store.update("mcp.json", server_id, enabled=body.enabled)
    if record is None:
        raise HTTPException(404, "no such server")
    return _public_mcp(record)
```

> - **`_public_mcp`**：`bearer_token` 换成 `True`；**另外补上 `kind`**（下面讲）。
> - `GET /api/mcp`、`add`、`toggle` 的返回值都经过它。

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

```python
def test_a_bearer_token_is_never_sent_back_to_the_browser(console: TestClient) -> None:
    """`_public_mcp` draws the same line chapter 19's `_public` already draws
    for a provider's `api_key`."""
    console.post(
        "/api/mcp", json={"name": "gh", "url": "https://x.test/mcp", "bearer_token": "ghp_secret"}
    )
    body = console.get("/api/mcp").text
    assert "ghp_secret" not in body
    assert '"bearer_token":true' in body.replace(" ", "")
```

`kind` 那一行是这次改写加的。前端的 `types.ts`：

```typescript
export interface McpOAuthConfig {
  client_id: string | null;
  scope: string | null;
}

/** One `mcp.json` record. `kind` is the discriminator chapter 22 added
 * (F22-04): "stdio" carries `command`/`env`/`cwd`, "remote" carries `url` and
 * one of `bearer_token`/`oauth`. A record from before chapter 22 has no
 * `kind` at all, which is why the backend derives it rather than requiring
 * it -- the frontend only ever reads records the backend has already
 * normalised, so it can rely on `kind` being present. */
export interface McpServer {
  id: string;
  name: string;
  kind: "stdio" | "remote";
  // stdio only
  command?: string[];
  env?: Record<string, string>;
  cwd?: string | null;
  // remote only
  url?: string;
  // `true` once a token is on file; the raw value never reaches the browser.
  bearer_token?: boolean;
  http_headers?: Record<string, string>;
  env_http_headers?: Record<string, string>;
  oauth?: McpOAuthConfig | null;
  // both
```

注释说"前端只读后端已经规范化过的记录，所以可以依赖 `kind` 一定在"。可第一版的 `_public_mcp` 只换 token，**不补 `kind`**——一条第 22 章之前存的 stdio 记录原样发出去，没有 `kind`。卡片上那一行是这么写的：

```tsx
<div className="ic-meta">{m.kind === "stdio" ? (m.command ?? []).join(" ") : m.url}</div>
```

`kind` 是 `undefined`，不等于 `"stdio"`，于是显示 `m.url`——一条 stdio 记录没有 `url`，那一行是空的。第一版有一个测试叫 `test_a_pre_chapter22_stdio_record_with_no_kind_key_still_dispatches`（§9），它测的是**后端**的分派，而这句承诺是对**前端**做的。

```python
def test_a_record_saved_before_chapter_22_goes_out_with_its_kind(
    console: TestClient,
) -> None:
    """`types.ts` promises the frontend a `kind` on every record; a stdio
    record saved before chapter 22 has none on disk."""
    store = console.app.state.console.store  # type: ignore[attr-defined]
    store.add("mcp.json", {"name": "old", "command": ["true"], "enabled": True})
    (record,) = console.get("/api/mcp").json()
    assert record["kind"] == "stdio"
```

### 7.1 改写时发现：删掉的服务器，token 留给了下一个同名的

`delete_mcp`（第一版）：

```python
@router.delete("/api/mcp/{server_id}")
def delete_mcp(request: Request, server_id: str) -> dict[str, bool]:
    return {"ok": _console(request).store.delete("mcp.json", server_id)}
```

只删记录。token 还在 `mcp_tokens.json` 里，挂在那个**名字**下面。量一下：存一条 `gh`（GitHub）、连好、删掉；再加一条也叫 `gh`、指向另一个服务器的记录：

```
$ uv run python reuse22.py
delete: {'ok': True}
status of the new record: {'connected': True}
the other server received: Bearer gho_github_token
```

（`reuse22.py`：通过控制台的路由加、删、再加；"连好"那一步直接把一个 token 写进存储；最后用一轮对话会用的 `resolve_mcp_configs` 加 `McpClient` 连新的那个，看假服务器收到了什么。）

新记录从来没有连过，界面上却显示"已连接"；它的第一个请求带着 **GitHub 的 token** 发给了另一台主机。这和 §6.1 是同一件事的两个入口：token 按名字存，名字和服务器之间的对应关系，程序没有守住。

修法：删一个远程服务器时，把它名下的 token 和客户端信息一起忘掉（第 21 章 `OwnerTokenStorage.forget`）：

```python
@router.delete("/api/mcp/{server_id}")
async def delete_mcp(request: Request, server_id: str) -> dict[str, bool]:
    """Remove a server, and the OAuth token and client stored under its name.

    The first version removed only the record.  The token stayed in
    `mcp_tokens.json` under the name, so the next server added with that name
    -- any URL at all -- showed "Connected" without ever connecting, and its
    first request carried the old server's token.  Measured when chapter 22
    was rewritten: a GitHub token, sent to a different host.
    """
    console = _console(request)
    record = console.store.get("mcp.json", server_id)
    ok = console.store.delete("mcp.json", server_id)
    still_named = any(
        r.get("name") == (record or {}).get("name") for r in console.store.all("mcp.json")
    )
    if ok and record is not None and record_kind(record) == "remote" and not still_named:
        # (`still_named`: a same-named record saved before names were unique
        # still uses that token.)
        try:
            await OwnerTokenStorage(DEFAULT_OWNER, server=record["name"]).forget()
        except OAuthStorageError as exc:
            raise HTTPException(500, f"server removed, but its token was not: {exc}") from exc
    return {"ok": ok}
```

> - 变成了 `async`：`forget()` 是异步的。
> - `still_named`：如果还有一条同名记录（名字唯一之前存下的），它还在用这个 token，不删。
> - 删不掉（第 21 章改写后，坏掉的 token 文件会抛 `OAuthStorageError`）：500，并且说清楚"记录删了，token 没删"。

```python
async def test_deleting_a_server_forgets_its_token(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured when chapter 22 was rewritten: delete "gh", add a new "gh"
    at another URL, and the new record read "Connected" -- and its first
    request carried the old server's token."""
    async with _async_api(tmp_path, owner, monkeypatch) as api:
        old = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": "https://a.test/mcp",
                    "oauth": True,
                    "oauth_client_id": "c",
                },
            )
        ).json()
        await OwnerTokenStorage(owner, server="gh").set_tokens(OAuthToken(access_token="gho_old"))
        assert (await api.delete(f"/api/mcp/{old['id']}")).json() == {"ok": True}
        assert await OwnerTokenStorage(owner, server="gh").get_tokens() is None

        new = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": "https://b.test/mcp",
                    "oauth": True,
                    "oauth_client_id": "c",
                },
            )
        ).json()
        status = (await api.get(f"/api/mcp/{new['id']}/oauth/status")).json()
        assert status == {"connected": False, "token": None}
```

> 删掉以后存储里没有 token；再加一个同名的，状态是"没连接"、没有 token 指纹。

还有一个这一章没有修的入口：命令行（第 21 章）和控制台都用 `DEFAULT_OWNER`，同一个 `mcp_tokens.json`。命令行配置文件里一个叫 `gh` 的服务器和控制台里一个叫 `gh` 的服务器，共用一个 token。要彻底解决，token 得记住它是为哪个 URL 发的；这一章没有做，写在这里。

---

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
不需要真弹窗；第一版以为也不需要真时钟，其实需要（§8.2）。

`frontend/src/mcpOauth.ts` 全文（这次改写改过的版本）：

```typescript
// Driving one MCP OAuth connection from the browser side.
//
// There is no `window.opener` handshake here on purpose. `window.open(url,
// "_blank", "noopener")` is what the component below actually calls -- and
// `noopener` is not incidental: it also means the new tab has no reference
// back to open a `postMessage` channel with, so relying on one would mean
// choosing a mechanism that only works when a *different*, less safe, call is
// made. Polling `GET /api/mcp/{id}/oauth/status` instead survives a popup
// blocker, a closed tab, and a page reload equally, and it is what the
// backend's own callback route (`routes.py`) was written to make sufficient:
// its whole job is to make the token show up there, not to talk back to a
// window it cannot be sure still exists.
//
// Written as one plain async function with every side effect injected,
// rather than a hook, so `mcpOauth.test.ts` can drive it with fakes and a
// zero-delay `sleep` instead of a real popup and a real clock.

export type OAuthConnectPhase = "starting" | "waiting" | "connected" | "timed_out" | "error";

// `previous` and `token` are fingerprints of the stored token (`routes.py`),
// before this attempt and now.  "Connected" means a token is on file that was
// not on file when the attempt started -- not merely that some token is.  The
// first version polled `connected` alone, so Reconnect on a server that
// already had a (dead) token reported success on its first poll, before the
// person had done anything.  Found when chapter 22 was rewritten.
export interface OAuthConnectDeps {
  start: () => Promise<{ authorize_url: string; previous?: string | null }>;
  status: () => Promise<{ connected: boolean; token?: string | null }>;
  openTab: (url: string) => void;
  sleep?: (ms: number) => Promise<void>;
  // The clock the deadline reads.  Injected for the same reason `sleep` is:
  // the first version's timeout test advanced a counter through a fake
  // `sleep` while the deadline read the real `Date.now()`, so it was really
  // a 25ms busy loop on the wall clock.
  now?: () => number;
  pollIntervalMs?: number;
  timeoutMs?: number;
}

const DEFAULT_POLL_INTERVAL_MS = 1500;
// Long enough to read a real consent screen and click through it -- the same
// order of magnitude as the backend's own `PendingOAuthTable` TTL (10
// minutes), a little shorter because a person still watching this tab after
// two minutes of silence is more likely stuck than about to finish.
const DEFAULT_TIMEOUT_MS = 120_000;

export async function runOAuthConnect(
  deps: OAuthConnectDeps,
  onPhase: (phase: OAuthConnectPhase, detail?: string) => void,
): Promise<void> {
  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const interval = deps.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS;
  const timeout = deps.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const now = deps.now ?? (() => Date.now());

  onPhase("starting");
  let authorizeUrl: string;
  let previous: string | null;
  try {
    const started = await deps.start();
    authorizeUrl = started.authorize_url;
    previous = started.previous ?? null;
  } catch (exc) {
    onPhase("error", (exc as Error).message);
    return;
  }

  deps.openTab(authorizeUrl);
  onPhase("waiting");

  const deadline = now() + timeout;
  while (now() < deadline) {
    await sleep(interval);
    try {
      const { connected, token } = await deps.status();
      // A status without `token` (an older backend) can only say "a token
      // exists"; take that as it is rather than wait for a field that will
      // never arrive.
      if (connected && (token === undefined || token !== previous)) {
        onPhase("connected");
        return;
      }
    } catch {
      // A status check that fails once is not the same claim as OAuth having
      // failed -- keep polling until the deadline instead of ending the
      // attempt on one dropped request.
    }
  }
  onPhase("timed_out");
}
```

> - **`OAuthConnectPhase`**：五个阶段，界面按它显示状态。
> - **`OAuthConnectDeps`**：所有副作用——开始、查状态、开标签页、睡一会儿、读时钟——都由调用者传进来。测试传假的，界面传真的。
> - **`runOAuthConnect`**：`start()`（失败就是 `error`，不开标签页）→ 开标签页 → `waiting` → 循环：睡一会儿、问一次状态、"连上了而且是一个新 token"就 `connected`；某一次问失败了不算，接着问；到了截止时间就 `timed_out`。
> - `previous`/`token`、`now` 这几处是这次改写改的，下面两节。

`api.ts` 里对应的两个调用：

```typescript
startMcpOauth: (serverId: string) =>
  call<{ authorize_url: string; previous: string | null }>("POST", "/api/mcp/oauth/start", {
    server_id: serverId,
  }),
mcpOauthStatus: (serverId: string) =>
  call<{ connected: boolean; token: string | null }>("GET", `/api/mcp/${serverId}/oauth/status`),
```

状态路由：

```python
@router.get("/api/mcp/{server_id}/oauth/status")
async def mcp_oauth_status(request: Request, server_id: str) -> dict[str, Any]:
    """Whether this server has a stored token -- what the console polls after
    opening the authorize URL in a new tab, instead of depending on that tab
    successfully messaging back to a window it may not still have a handle
    to (a popup blocker, `noopener`, or the tab simply being closed early all
    break a `postMessage`-based handshake; polling a status a person can also
    just reload the page and still see does not).

    `token` is a fingerprint of the stored token.  "Is there a token?" alone
    cannot tell a *new* token from the one already on file, and the first
    version's Reconnect -- the button the runtime's own error message sends
    people to when a session has to be renewed -- read the old token on its
    first poll and reported "connected" before anyone had clicked anything.
    Found when chapter 22 was rewritten.
    """
    console = _console(request)
    record = _mcp_record(console, server_id)
    storage = OwnerTokenStorage(DEFAULT_OWNER, server=record["name"])
    tokens = await storage.get_tokens()
    return {"connected": tokens is not None, "token": _token_fingerprint(tokens)}
```

前端测试：

```typescript
import { describe, expect, it } from "vitest";
import { runOAuthConnect } from "./mcpOauth";

// `sleep` is faked out with an immediately-resolving promise in every test
// here, so "poll a few times" costs microtasks rather than wall-clock time --
// the same reason `test_faults_ch19.py`'s fakes never actually wait on a
// socket.
const instant = () => Promise.resolve();

describe("runOAuthConnect", () => {
  it("opens the authorize URL and reports connected once status flips", async () => {
    const opened: string[] = [];
    let calls = 0;
    const phases: string[] = [];

    await runOAuthConnect(
      {
        start: async () => ({ authorize_url: "https://auth.example/authorize?x=1" }),
        status: async () => {
          calls += 1;
          return { connected: calls >= 3 };
        },
        openTab: (url) => opened.push(url),
        sleep: instant,
      },
      (phase) => phases.push(phase),
    );

    expect(opened).toEqual(["https://auth.example/authorize?x=1"]);
    expect(calls).toBe(3);
    expect(phases).toEqual(["starting", "waiting", "connected"]);
  });

  it("reports error and never opens a tab when start() fails", async () => {
    const opened: string[] = [];
    const phases: [string, string | undefined][] = [];

    await runOAuthConnect(
      {
        start: async () => {
          throw new Error("no client_id and this server has no registration_endpoint");
        },
        status: async () => ({ connected: false }),
        openTab: (url) => opened.push(url),
        sleep: instant,
      },
      (phase, detail) => phases.push([phase, detail]),
    );

    expect(opened).toEqual([]);
    expect(phases).toEqual([
      ["starting", undefined],
      ["error", "no client_id and this server has no registration_endpoint"],
    ]);
  });

  it("times out rather than polling forever when nobody finishes authorizing", async () => {
    const phases: string[] = [];
    let now = 0;
    let polls = 0;
    // A fake clock: `sleep` advances it, and `now` is what the deadline reads
    // -- so the budget of 25 is three polls of 10, with no real time passing.

    await runOAuthConnect(
      {
        start: async () => ({ authorize_url: "https://auth.example/authorize" }),
        status: async () => {
          polls += 1;
          return { connected: false };
        },
        openTab: () => undefined,
        sleep: async () => {
          now += 10;
        },
        now: () => now,
        pollIntervalMs: 10,
        timeoutMs: 25,
      },
      (phase) => phases.push(phase),
    );

    expect(polls).toBe(3);
    expect(now).toBe(30);
    expect(phases.at(-1)).toBe("timed_out");
  });

  it("keeps polling through a transient status failure instead of giving up", async () => {
    const phases: string[] = [];
    let calls = 0;

    await runOAuthConnect(
      {
        start: async () => ({ authorize_url: "https://auth.example/authorize" }),
        status: async () => {
          calls += 1;
          if (calls === 1) throw new Error("network blip");
          return { connected: calls >= 2 };
        },
        openTab: () => undefined,
        sleep: instant,
      },
      (phase) => phases.push(phase),
    );

    expect(calls).toBe(2);
    expect(phases).toEqual(["starting", "waiting", "connected"]);
  });

  it("on Reconnect, waits for a different token than the one already on file", async () => {
    // The token on file before the attempt is "old"; the first two polls still
    // see it, the third sees the new one.  The first version reported
    // "connected" on the first poll.
    const phases: string[] = [];
    let calls = 0;

    await runOAuthConnect(
      {
        start: async () => ({ authorize_url: "https://auth.example/authorize", previous: "old" }),
        status: async () => {
          calls += 1;
          return { connected: true, token: calls >= 3 ? "new" : "old" };
        },
        openTab: () => undefined,
        sleep: instant,
      },
      (phase) => phases.push(phase),
    );

    expect(calls).toBe(3);
    expect(phases).toEqual(["starting", "waiting", "connected"]);
  });
});
```

> - `instant`：一个立刻完成的 `sleep`——"轮询几次"只花几个微任务，不花真实时间。
> - 第一个：第三次查状态时连上了；断言开过一次标签页、查了三次、阶段依次是 `starting → waiting → connected`。
> - 第二个：`start()` 失败，不开标签页，阶段是 `error` 加上原因。
> - 第三个（超时）、第五个（重新连接）：下面两节。
> - 第四个：第一次查状态抛错，第二次连上——中间那次失败不终止整个流程。

```
$ npx vitest run src/mcpOauth.test.ts
 ✓ src/mcpOauth.test.ts > runOAuthConnect > opens the authorize URL and reports connected once status flips 2ms
 ✓ src/mcpOauth.test.ts > runOAuthConnect > reports error and never opens a tab when start() fails 0ms
 ✓ src/mcpOauth.test.ts > runOAuthConnect > times out rather than polling forever when nobody finishes authorizing 0ms
 ✓ src/mcpOauth.test.ts > runOAuthConnect > keeps polling through a transient status failure instead of giving up 0ms
 ✓ src/mcpOauth.test.ts > runOAuthConnect > on Reconnect, waits for a different token than the one already on file 0ms
 Test Files  1 passed (1)
      Tests  5 passed (5)
```

### 8.1 改写时发现："重新连接"在用户点同意之前就成功了

卡片上那个按钮的文字是 `{isConnected ? "Reconnect" : "Connect"}`——已经连过的服务器显示"Reconnect"。第 22 章的 `runtime.py`（§9）在 token 失效时给用户的提示也是"去 MCP 页签里点 Connect"。所以**重新连接一个已经有（失效的）token 的服务器**是这个按钮的主要用法之一。

第一版的状态路由只回答 `{"connected": tokens is not None}`，前端只看 `connected`：

```ts
      const { connected } = await deps.status();
      if (connected) {
        onPhase("connected");
```

旧 token 还在文件里——第一次轮询就是 `true`。界面显示"已连接"，用户关掉标签页；那个失效的 token 原封不动。

修法：状态里多带一个 token 的**指纹**（`_token_fingerprint`：SHA-256 的前 16 个十六进制字符——两个不同的 token 指纹几乎不可能相同，从指纹也推不回 token），`start` 的返回值里带上**开始之前**那个 token 的指纹（`previous`）。前端等的是"连上了，而且指纹和开始时不一样"。

一个细节：如果后端没有发 `token` 字段（一个老版本的后端），`token === undefined`，就退回只看 `connected`——否则会永远等一个不会来的字段。这个细节是改写时自己踩出来的：第一次改的时候写成了 `(token ?? null) !== previous`，老的四个测试（假状态里没有 `token`）全部卡死——`null !== null` 永远是假，循环里的 `sleep` 是立刻完成的微任务，连 vitest 自己的超时都没机会触发，整个测试进程挂住。

测试就是上面第五个：开始前的 token 是 `old`，前两次轮询还是 `old`，第三次是 `new`；断言查了三次才连上。后端那一半：

```python
async def test_reconnect_reports_a_new_token_not_the_old_one(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start` says which token was on file before; the status after the
    callback carries a different one.  The first version's status was only
    `{"connected": true}`, which an old, dead token answers just as well."""
    async with (
        OAuthStub(supports_dcr=False) as auth,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        created = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": auth.resource_url,
                    "oauth": True,
                    "oauth_client_id": "p",
                },
            )
        ).json()
        await OwnerTokenStorage(owner, server="gh").set_tokens(OAuthToken(access_token="dead"))
        status_before = (await api.get(f"/api/mcp/{created['id']}/oauth/status")).json()
        started = (await api.post("/api/mcp/oauth/start", json={"server_id": created["id"]})).json()
        assert started["previous"] == status_before["token"] is not None

        async with httpx2.AsyncClient() as browser:
            response = await browser.get(started["authorize_url"], follow_redirects=False)
        params = parse_qs(urlparse(response.headers["location"]).query)
        await api.get(
            "/api/mcp/oauth/callback",
            params={"code": params["code"][0], "state": params["state"][0]},
        )
        status_after = (await api.get(f"/api/mcp/{created['id']}/oauth/status")).json()
    assert status_after["token"] not in (None, started["previous"])
```

> 先存一个 `dead` token；`start` 返回的 `previous` 等于那时状态里的指纹；走完回调以后，指纹既不是 `None` 也不是 `previous`。

### 8.2 改写时发现：超时测试读的是真时钟

第一版的超时测试注释说：

```ts
    // A fake clock local to this test: `Date.now` is not stubbed globally,
    // `sleep` just advances a counter the deadline math reads through
    // `pollIntervalMs`/`timeoutMs` set small enough that two polls elapse the
    // budget without a real timer ever running.
```

可 `runOAuthConnect` 的截止时间读的是 `Date.now()`，那个计数器 `now` 它根本看不见。测试能通过，是因为它在一个 `sleep` 立刻完成的循环里**空转了 25 毫秒的真实时间**。第一版的代码和测试原样跑一次，耗时正好是那个预算：

```
 ✓ src/mcpOauth.test.ts > runOAuthConnect > times out rather than polling forever when nobody finishes authorizing 25ms
```

断言也只有 `now > 0`——循环转了多少圈，取决于机器多快。

修法：时钟也注入（`deps.now`），测试传 `() => now`。现在 25 的预算正好是三次 10 的轮询，测试断言 `polls === 3`、`now === 30`。上面那段 vitest 输出里，这个测试的耗时从 `25ms` 变成了 `0ms`。

### 8.3 界面那一侧：`Extensions.tsx`

文件开头的 GitHub 地址：

```tsx
// GitHub is this chapter's one measured remote server (F21-06/07/08's own
// numbers, cited rather than re-derived): its MCP endpoint, for the
// "Connect GitHub" shortcut below. Nothing about the backend knows this
// string is special -- the shortcut only pre-fills the same generic
// remote-server form everyone else's URL goes through.
const GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/";
```

MCP 页签的表单状态、几个函数：

```tsx
type AddMode = "local" | "remote";
type AuthMode = "token" | "oauth";

const EMPTY_FORM = {
  name: "",
  command: "",
  env: "",
  cwd: "",
  url: "",
  bearer_token: "",
  oauth_client_id: "",
  oauth_scope: "",
  startup_timeout: "30",
  tool_timeout: "60",
};

function Mcp({ servers, reload }: { servers: McpServer[]; reload: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<AddMode>("local");
  const [auth, setAuth] = useState<AuthMode>("token");
  const [error, setError] = useState("");
  const [form, setForm] = useState(EMPTY_FORM);

  // Whether each remote+OAuth server already has a token on file. Polled once
  // per server whenever the list changes rather than kept in `McpServer`
  // itself -- `GET /api/mcp` describes *configuration*, and connectedness is a
  // different, faster-changing question the backend answers separately
  // (`GET /api/mcp/{id}/oauth/status`).
  const [connected, setConnected] = useState<Record<string, boolean>>({});
  // Live progress for a "Connect" click in *this* browser tab -- not
  // persisted, not shared with `connected`, and gone the moment the click
  // resolves one way or the other.
  const [connecting, setConnecting] = useState<Record<string, { phase: OAuthConnectPhase; detail?: string }>>(
    {},
  );

  useEffect(() => {
    const targets = servers.filter((s) => s.kind === "remote" && s.oauth);
    if (targets.length === 0) return;
    let cancelled = false;
    void Promise.all(
      targets.map(async (s) => {
        try {
          const { connected: ok } = await api.mcpOauthStatus(s.id);
          if (!cancelled) setConnected((prev) => ({ ...prev, [s.id]: ok }));
        } catch {
          // Left unknown rather than assumed disconnected -- the card just
          // shows no chip until the next successful check.
        }
      }),
    );
    return () => {
      cancelled = true;
    };
  }, [servers]);

  const resetForm = () => {
    setForm(EMPTY_FORM);
    setMode("local");
    setAuth("token");
  };

  const openGithubShortcut = () => {
    setError("");
    setMode("remote");
    setAuth("token");
    setForm((f) => ({ ...f, name: f.name || "github", url: GITHUB_MCP_URL }));
    setOpen(true);
  };

  const save = async () => {
    setError("");
    if (!form.name.trim()) {
      setError("name is required");
      return;
    }
    if (mode === "local") {
      if (!form.command.trim()) {
        setError("command is required");
        return;
      }
      // `KEY=value` per line -- the shape a person already knows from a
      // shell, and the shape `ServerConfig.env` wants.
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
    } else {
      if (!form.url.trim()) {
        setError("url is required");
        return;
      }
      try {
        await api.addMcp({
          name: form.name.trim(),
          url: form.url.trim(),
          bearer_token: auth === "token" ? form.bearer_token.trim() || undefined : undefined,
          oauth: auth === "oauth",
          oauth_client_id: auth === "oauth" ? form.oauth_client_id.trim() || undefined : undefined,
          oauth_scope: auth === "oauth" ? form.oauth_scope.trim() || undefined : undefined,
          startup_timeout: Number(form.startup_timeout),
          tool_timeout: Number(form.tool_timeout),
        });
      } catch (exc) {
        setError((exc as Error).message);
        return;
      }
    }
    resetForm();
    setOpen(false);
    await reload();
  };

  const connect = (server: McpServer) => {
    setConnecting((prev) => ({ ...prev, [server.id]: { phase: "starting" } }));
    void runOAuthConnect(
      {
        start: () => api.startMcpOauth(server.id),
        status: () => api.mcpOauthStatus(server.id),
        // `noopener`: the new tab gets no handle back to this one, which is
        // also why this console polls for the result instead of waiting on a
        // `postMessage` the tab would have no way to send.
        openTab: (url) => window.open(url, "_blank", "noopener"),
      },
      (phase, detail) => {
        setConnecting((prev) => ({ ...prev, [server.id]: { phase, detail } }));
        if (phase === "connected") {
          setConnected((prev) => ({ ...prev, [server.id]: true }));
          void reload();
        }
      },
    );
  };

```

> - **`EMPTY_FORM`**：两种形状的字段放在一个对象里，切换"本地/远程"不丢已经填的内容。
> - **`connected`**：每个配了 OAuth 的远程服务器有没有 token——`useEffect` 在服务器列表变化时各问一次状态。**`connecting`**：这个标签页里正在进行的连接的阶段。
> - **`openGithubShortcut`**："Connect GitHub" 按钮：切到远程、选 token、名字填 `github`、网址填 GitHub 的——只是预填同一张表单。
> - **`save`**：本地的把 `KEY=value` 一行一行拆成字典；远程的按认证方式只发相应的字段。
> - **`connect`**：调 `runOAuthConnect`，把真的 `api` 调用和 `window.open(..., "noopener")` 交给它；连上以后刷新列表。

界面本身：

```tsx
  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "46ch" }}>
          Outside tool servers minicodex can call over MCP. Each server's tools load under its own
          namespace, so two servers can both offer <code>search</code>. A local command runs as a
          subprocess of this server; a remote server is somebody else's HTTPS endpoint, reached
          with a bearer token or an OAuth login.
        </p>
        <div style={{ display: "flex", gap: "var(--sp-2)" }}>
          <button className="btn btn-ghost btn-sm" onClick={openGithubShortcut}>
            Connect GitHub
          </button>
          <button className="btn btn-sm" onClick={() => setOpen((v) => !v)}>
            + Add server
          </button>
        </div>
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

        <div className="radio-row">
          <button
            type="button"
            className={`radio-pill${mode === "local" ? " on" : ""}`}
            onClick={() => setMode("local")}
          >
            Local command
          </button>
          <button
            type="button"
            className={`radio-pill${mode === "remote" ? " on" : ""}`}
            onClick={() => setMode("remote")}
          >
            Remote server
          </button>
        </div>

        {mode === "local" ? (
          <>
            <div className="form-row">
              <label htmlFor="m-cmd">Command</label>
              <input
                id="m-cmd"
                placeholder="npx @org/mcp-server-name --flag"
                value={form.command}
                onChange={(e) => setForm({ ...form, command: e.target.value })}
              />
            </div>
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
            <div className="form-row">
              <label htmlFor="m-cwd">Working directory</label>
              <input
                id="m-cwd"
                placeholder="optional"
                value={form.cwd}
                onChange={(e) => setForm({ ...form, cwd: e.target.value })}
              />
            </div>
          </>
        ) : (
          <>
            <div className="form-row">
              <label htmlFor="m-url">URL</label>
              <input
                id="m-url"
                placeholder="https://example.com/mcp"
                value={form.url}
                onChange={(e) => setForm({ ...form, url: e.target.value })}
              />
            </div>

            <div className="radio-row">
              <button
                type="button"
                className={`radio-pill${auth === "token" ? " on" : ""}`}
                onClick={() => setAuth("token")}
              >
                Personal access token
              </button>
              <button
                type="button"
                className={`radio-pill${auth === "oauth" ? " on" : ""}`}
                onClick={() => setAuth("oauth")}
              >
                OAuth login
              </button>
            </div>

            {auth === "token" ? (
              <div className="form-row">
                <label htmlFor="m-token">Bearer token</label>
                <input
                  id="m-token"
                  type="password"
                  placeholder="pasted once, kept on this server, never shown again"
                  value={form.bearer_token}
                  onChange={(e) => setForm({ ...form, bearer_token: e.target.value })}
                />
              </div>
            ) : (
              <>
                <p className="settings-help">
                  Dynamic client registration (RFC 7591) does not work against every authorization
                  server -- GitHub's does not advertise it. Leave client ID blank to try dynamic
                  registration; for a server like GitHub's, its operator first registers an OAuth
                  App and puts the client ID here.
                </p>
                <div className="form-grid">
                  <div className="form-row">
                    <label htmlFor="m-client">Client ID (optional)</label>
                    <input
                      id="m-client"
                      placeholder="from a pre-registered OAuth App"
                      value={form.oauth_client_id}
                      onChange={(e) => setForm({ ...form, oauth_client_id: e.target.value })}
                    />
                  </div>
                  <div className="form-row">
                    <label htmlFor="m-scope">Scope (optional)</label>
                    <input
                      id="m-scope"
                      placeholder="space-separated"
                      value={form.oauth_scope}
                      onChange={(e) => setForm({ ...form, oauth_scope: e.target.value })}
                    />
                  </div>
                </div>
              </>
            )}
          </>
        )}

        <div className="form-grid">
          <div className="form-row">
            <label htmlFor="m-start">Startup timeout (s)</label>
            <input
              id="m-start"
              value={form.startup_timeout}
              onChange={(e) => setForm({ ...form, startup_timeout: e.target.value })}
            />
          </div>
          <div className="form-row">
            <label htmlFor="m-tool">Tool call timeout (s)</label>
            <input
              id="m-tool"
              value={form.tool_timeout}
              onChange={(e) => setForm({ ...form, tool_timeout: e.target.value })}
            />
          </div>
        </div>
        <div className="form-actions">
          <span className="form-error">{error}</span>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => {
              setOpen(false);
              resetForm();
            }}
          >
            Cancel
          </button>
          <button className="btn btn-primary btn-sm" onClick={() => void save()}>
            Add server
          </button>
        </div>
      </div>

      <div className="card-list">
        {servers.map((m) => {
          const progress = connecting[m.id];
          const needsOauth = m.kind === "remote" && !!m.oauth;
          const isConnected = connected[m.id];
          return (
            <div className="item-card" key={m.id}>
              <div className="ic-main">
                <div className="ic-title-row">
                  <span className="ic-name">{m.name}</span>
                  {m.kind === "remote" && <span className="ic-active-tag">remote</span>}
                </div>
                <div className="ic-meta">{m.kind === "stdio" ? (m.command ?? []).join(" ") : m.url}</div>
              </div>
              <div className="ic-actions">
                {m.kind === "remote" && m.bearer_token && <StatusChip tone="ok" text="Token set" />}
                {needsOauth && !progress && (
                  <StatusChip
                    tone={isConnected ? "ok" : "info"}
                    text={isConnected ? "Connected" : "Not connected"}
                  />
                )}
                {progress && progress.phase !== "connected" && (
                  <StatusChip
                    tone={progress.phase === "error" ? "danger" : "info"}
                    text={
                      progress.phase === "waiting"
                        ? "Waiting for authorization..."
                        : progress.phase === "timed_out"
                          ? "Timed out"
                          : progress.phase === "error"
                            ? `Failed: ${progress.detail ?? "unknown error"}`
                            : "Starting..."
                    }
                  />
                )}
                {needsOauth && (
                  <button className="btn btn-ghost btn-sm" onClick={() => connect(m)}>
                    {isConnected ? "Reconnect" : "Connect"}
                  </button>
                )}
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
          );
        })}
      </div>
    </div>
  );
}
```

> - 工具栏两个按钮："Connect GitHub" 和 "+ Add server"。
> - 表单：名字；"Local command / Remote server" 两个选项；本地是命令、环境变量、工作目录；远程是网址，再分"Personal access token / OAuth login"——OAuth 那一边有一段说明（§11 引用它）。
> - 卡片：远程的有 `remote` 标签；有 token 显示"Token set"；配了 OAuth 的显示"Connected/Not connected"或者连接进行中的阶段，以及"Connect/Reconnect"按钮。`ic-meta` 那一行就是 §7 说的、`kind` 缺失时显示空白的那一行。

## §9 一个没有浏览器的进程不能假装有

回到 F22-01 的另一半。一轮对话开始时，`runtime.py` 要把存下来的记录变成
`connect()` 要的 config 列表。如果有一台远程服务器配了 OAuth 但**还没连过**呢？

朴素的做法是照连不误，让它失败。但失败在哪里？在 SDK 的 401 分支里，
那里会去调 `redirect_handler`——而一个跑在服务器上的进程能传进去的
`redirect_handler` 只有一个会抛异常的版本。

所以这一轮**明确地不连它**，并且说人话：


两个"拒绝"的 handler 和 `resolve_mcp_configs` 的后半段：

```python
async def _oauth_redirect_refused(url: str) -> None:
    """The `redirect_handler` a turn's `connect()` call is given for any
    remote server with OAuth configured.

    It is never actually reached in practice -- `run_turn` only ever hands
    `connect()` a config whose token storage already has a token (see below)
    -- and it exists so that if a stored token and its refresh token both stop
    working mid-turn, `OAuthClientProvider`'s 401 branch fails loudly with a
    sentence pointing at the console's own Connect button instead of trying to
    open a browser on the server (F22-01, again: that is exactly the thing
    this chapter exists because chapter 21's default cannot do here).
    """
    raise RuntimeError(
        "this server's OAuth session needs to be renewed from the console's "
        "MCP tab -- open Connect there, then send the message again"
    )


async def _oauth_callback_refused() -> Any:
    raise RuntimeError("no interactive OAuth callback exists inside a turn")
```

> - **`_oauth_redirect_refused`**：一轮对话里如果真走到了要开浏览器的地方（token 和 refresh token 都失效了），抛一句指向 Connect 按钮的话，而不是在服务器上开浏览器。
> - 第 21 章改写之后，这句话会原样出现在 `could not start ...: RuntimeError: ...` 里（第 21 章 §4.1），而不是 "unhandled errors in a TaskGroup"。

`resolve_mcp_configs`（§6.2 引过全文）的后半段：OAuth 服务器没有 token，就发一条 `needs_connection`，不连它。`run_turn` 里调用它的地方：

```python
configs = await resolve_mcp_configs(enabled, emit)
clients = (
    await connect(
        configs,
        registry,
        handlers={"elicitation/create": elicitation_handler(session)},
        redirect_handler=_oauth_redirect_refused,
        callback_handler=_oauth_callback_refused,
    )
    if configs
    else []
)
for name, reason in registry.failures.items():
    emit({"type": "mcp_status", "name": name, "status": "error", "detail": reason})
```

这不是一条新故障，是 F22-01 的同一个事实换个位置出现：**一个没有浏览器的
进程不能假装它能给人看一个页面。** 能做的是说清楚该去点哪里。

测试：

```python
async def test_a_remote_oauth_server_without_a_token_is_skipped_with_a_clear_status(
    owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_runtime, "DEFAULT_OWNER", owner)
    events: list[dict[str, Any]] = []
    record = {
        "kind": "remote",
        "name": "gh",
        "url": "https://x.test/mcp",
        "oauth": {"client_id": None, "scope": None},
    }
    configs = await web_runtime.resolve_mcp_configs([record], events.append)
    assert configs == []
    assert events == [
        {
            "type": "mcp_status",
            "name": "gh",
            "status": "needs_connection",
            "detail": "not connected yet -- use Connect in the MCP tab",
        }
    ]


async def test_a_remote_oauth_server_with_a_token_on_file_is_connected_normally(
    owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web_runtime, "DEFAULT_OWNER", owner)
    await OwnerTokenStorage(owner, server="gh").set_tokens(OAuthToken(access_token="a"))
    events: list[dict[str, Any]] = []
    record = {
        "kind": "remote",
        "name": "gh",
        "url": "https://x.test/mcp",
        "oauth": {"client_id": None, "scope": None},
    }
    configs = await web_runtime.resolve_mcp_configs([record], events.append)
    assert len(configs) == 1
    assert configs[0].name == "gh"
    assert events == []


async def test_a_stdio_server_is_unaffected_by_any_of_this() -> None:
    events: list[dict[str, Any]] = []
    record = {"kind": "stdio", "name": "notes", "command": ["python", "-m", "server"]}
    configs = await web_runtime.resolve_mcp_configs([record], events.append)
    assert len(configs) == 1
    assert configs[0].command == ("python", "-m", "server")
    assert events == []


async def test_a_pre_chapter22_stdio_record_with_no_kind_key_still_dispatches() -> None:
    events: list[dict[str, Any]] = []
    record = {"name": "notes", "command": ["python", "-m", "server"], "env": {}, "cwd": None}
    configs = await web_runtime.resolve_mcp_configs([record], events.append)
    assert len(configs) == 1
    assert events == []
```

### 9.1 改写时发现：连上之后，第一轮对话就会失败

第一版最长的那个测试叫 `test_the_full_state_to_callback_to_token_storage_chain`——"状态 → 回调 → token 存储"，它的最后一句是 token 在磁盘上：

```python
async def test_the_full_state_to_callback_to_token_storage_chain(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        OAuthStub(supports_dcr=False) as auth,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        created = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": auth.resource_url,
                    "oauth": True,
                    "oauth_client_id": "preset-client",
                },
            )
        ).json()
        assert created["kind"] == "remote"

        started = await api.post("/api/mcp/oauth/start", json={"server_id": created["id"]})
        assert started.status_code == 200, started.text
        authorize_url = started.json()["authorize_url"]

        assert started.json()["previous"] is None
        before = (await api.get(f"/api/mcp/{created['id']}/oauth/status")).json()
        assert before == {"connected": False, "token": None}

        # A real network hop, to the stub authorization server -- unlike
        # `api`, which never leaves this process.
        async with httpx2.AsyncClient() as browser:
            response = await browser.get(authorize_url, follow_redirects=False)
        location = response.headers["location"]
        params = parse_qs(urlparse(location).query)

        callback = await api.get(
            "/api/mcp/oauth/callback",
            params={"code": params["code"][0], "state": params["state"][0]},
        )
        assert callback.status_code == 200
        assert "connected to gh" in callback.text

        after = (await api.get(f"/api/mcp/{created['id']}/oauth/status")).json()
        assert after["connected"] is True
        assert after["token"] is not None

    tokens = await OwnerTokenStorage(owner, server="gh").get_tokens()
    assert tokens is not None and tokens.access_token == "access-1"
```

（`previous`、`token` 那几行断言是这次改写改的，因为状态路由的返回值多了一个字段。）

链路停在这里。可用户点 Connect 不是为了让一个文件出现在磁盘上，是为了**下一轮对话能用这个服务器**。往后多走一步：用一轮对话会用的 `resolve_mcp_configs` 和 `connect()`、带着那两个拒绝的 handler，连一个真的 MCP 假服务器——

在第一版的代码上，这一步失败：第 21 章的 `McpClient` 把"这是谁的 token"存进 `_owner`，两行之后被同名的连接任务盖成 `None`（第 21 章 §8.4）。把第 22 步的 `mcp.py` 换回第 21 章修之前的那一版，跑下面那个测试：

```
E               assert {'gh': "could...'mcp_tokens'"} == {}
E                 Left contains 1 more item:
E                 {'gh': "could not start gh: 'NoneType' object has no attribute 'mcp_tokens'"}
```
**第一版的"在控制台里连 GitHub"，连上以后每一轮对话都连不上它。** 这个 bug 在第 21 章改写时修了；这个测试是在第 22 章这一侧把那条链路补完整：

```python
async def _connect_through_the_console(
    api: httpx2.AsyncClient, auth: OAuthStub, server_id: str
) -> None:
    """start -> the browser at the authorization server -> callback."""
    authorize_url = (await api.post("/api/mcp/oauth/start", json={"server_id": server_id})).json()[
        "authorize_url"
    ]
    async with httpx2.AsyncClient() as browser:
        response = await browser.get(authorize_url, follow_redirects=False)
    params = parse_qs(urlparse(response.headers["location"]).query)
    callback = await api.get(
        "/api/mcp/oauth/callback", params={"code": params["code"][0], "state": params["state"][0]}
    )
    assert "connected to" in callback.text, callback.text


async def test_a_server_connected_in_the_console_is_usable_in_a_turn(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain the tests above stop short of: Connect in the console, then
    a turn's own `resolve_mcp_configs` + `connect()` -- with the refusing
    handlers a turn passes -- reaching a real MCP endpoint with that token.

    The first version's tests ended at "the token is on disk".  Past that
    point, every turn would have failed: chapter 21's `McpClient` lost the
    `Owner` (fixed when chapter 21 was rewritten), and no test here went far
    enough to see it.

    The MCP endpoint and its authorization server are on different ports, and
    only the endpoint's 401 says where the metadata is -- the case `begin()`
    could not handle until it read that 401 (`_discover`)."""
    from mcp_http_stub import HttpMcpStub
    from minicodex.registry import McpRegistry, connect

    async with (
        OAuthStub(supports_dcr=False) as auth,
        HttpMcpStub(require_auth=True, challenge=auth.prm_url, accepts=auth.is_live) as mcp,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        auth.resource = mcp.url
        created = (
            await api.post(
                "/api/mcp",
                json={"name": "gh", "url": mcp.url, "oauth": True, "oauth_client_id": "preset"},
            )
        ).json()
        await _connect_through_the_console(api, auth, created["id"])

        record = api._transport.app.state.console.store.get("mcp.json", created["id"])  # type: ignore[attr-defined]
        events: list[dict[str, Any]] = []
        configs = await web_runtime.resolve_mcp_configs([record], events.append)
        assert events == []
        registry = McpRegistry()
        clients = await connect(
            configs,
            registry,
            owner=owner,
            redirect_handler=web_runtime._oauth_redirect_refused,
            callback_handler=web_runtime._oauth_callback_refused,
        )
        try:
            assert registry.failures == {}
            assert any("echo" in name for name in registry.handlers())
        finally:
            for client in clients:
                await client.close()
    assert mcp.authorized[-1] == "Bearer access-1"
```

> - **`_connect_through_the_console`**：一个帮手，`start` → 假浏览器去授权服务器 → `callback`。
> - 两个假服务器：`OAuthStub` 是授权服务器，`HttpMcpStub` 是 MCP 服务器，401 里点名 `OAuthStub` 的元数据（`challenge=auth.prm_url`），只接受 `OAuthStub` 发出去的 token（`accepts=auth.is_live`）。这是第一个把两者放在不同端口上的测试——§3.1 的问题就是它找到的。
> - 连完以后：从控制台的存储里取出记录，`resolve_mcp_configs` 没有发任何事件（没有"需要连接"），`connect()` 没有失败，工具 `echo` 在注册表里，MCP 服务器收到的最后一个 `Authorization` 是 `Bearer access-1`。

还有一个：第 21 章改写让坏掉的 token 文件变成 `OAuthStorageError`。回调在存 token 时遇到它，第一版会直接 500——浏览器看到的是一个内部错误，而不是那个页面：

```python
async def test_a_token_file_that_cannot_be_written_is_a_failed_page_not_a_500(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chapter 21's rewrite made a broken token file an `OAuthStorageError`
    rather than a silent overwrite; the callback has to say so in a page,
    not answer the browser with an internal error."""
    async with (
        OAuthStub(supports_dcr=False) as auth,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        created = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": auth.resource_url,
                    "oauth": True,
                    "oauth_client_id": "p",
                },
            )
        ).json()
        authorize_url = (
            await api.post("/api/mcp/oauth/start", json={"server_id": created["id"]})
        ).json()["authorize_url"]
        owner.mcp_tokens().write_text("{ not json", encoding="utf-8")
        async with httpx2.AsyncClient() as browser:
            response = await browser.get(authorize_url, follow_redirects=False)
        params = parse_qs(urlparse(response.headers["location"]).query)
        callback = await api.get(
            "/api/mcp/oauth/callback",
            params={"code": params["code"][0], "state": params["state"][0]},
        )
    assert callback.status_code == 200
    assert "Connection failed" in callback.text
    assert "not overwriting" in callback.text
```

> 授权中途把 token 文件写坏，回调页面显示"Connection failed"和"not overwriting"（第 21 章 §8.2 那句话）。`start` 和回调路由的 `except` 里都加了 `OAuthStorageError`。

---

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
| `src/minicodex/web/oauth.py` | 358 | `PendingOAuthTable`、`_challenge`/`_discover`、`begin()`/`finish()` |
| `src/minicodex/web/mcp_config.py` | 94 | 一条记录 → 一个 config，两处调用共用 |
| `web/routes.py` 的改动 | +278 | 两种表单形状、名字唯一、删除时忘掉 token、三个新路由、脱敏、转义 |
| `web/runtime.py` 的改动 | +88 | `resolve_mcp_configs`，按形状分派，坏记录跳过 |
| `frontend/.../Extensions.tsx` 的改动 | +356 | 本地/远程切换、两条认证路径、Connect GitHub |
| `frontend/src/mcpOauth.ts` | 90 | 轮询式连接流程，副作用全注入 |
| `tests/test_faults_ch22.py` | 776 | 33 个测试（第一版 24 个），含三条端到端链路 |
| `frontend/src/mcpOauth.test.ts` | 132 | 5 个前端测试（第一版 4 个） |
| `probe_mutations_ch22.py` | 223 | 20 条变异（第一版 12 条） |

（"的改动"是和第 21 步相比 `diff` 里新增和改动的行数。）

`mcp_config.py` 那 94 行值得单说一句：它只有两个函数，被 `routes.py` 和
`runtime.py` 各调一次。放在这里而不是塞进其中任何一个，是因为**两次是同一个
翻译，而写两遍的翻译一定会漂移**——第 3 章为提示词和 schema 里重复的工具描述
做过一模一样的修复。

测试文件的开头——说明、import、几个帮手：

```python
"""Chapter 22: a browser-driven remote MCP connection, from the console.

Every test here is offline, the same discipline chapter 21's own tests hold
to: `OAuthStub` (`tests/mcp_http_stub.py`) plays the authorization server,
`fastapi.testclient.TestClient` plays the browser talking to this console,
and a plain `httpx2` client plays the browser being redirected to the
authorization server and back -- nothing here resolves a real hostname.

`DEFAULT_OWNER` is patched to a `tmp_path`-backed `Owner` in every test that
reaches a route touching it (`console`'s own fixture does this once, for
every test that uses it): the real one resolves under the actual user's home
directory, and a web-layer test that skipped this would leave a stray
`mcp_tokens.json` under `~/.minicodex/` every time it ran.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client.auth.oauth2 import OAuthContext
from mcp.shared.auth import OAuthClientMetadata, OAuthToken

from mcp_http_stub import OAuthStub
from minicodex.mcp_oauth import OwnerTokenStorage
from minicodex.remote import RemoteConfigError, RemoteOAuthConfig, RemoteServerConfig
from minicodex.tenancy import Owner
from minicodex.web import create_app
from minicodex.web import runtime as web_runtime
from minicodex.web.mcp_config import record_kind, server_config
from minicodex.web.oauth import PendingOAuth, PendingOAuthTable, begin, finish


@pytest.fixture
def owner(tmp_path: Path) -> Owner:
    return Owner(home=tmp_path / "home")


def _patch_owner(owner: Owner, monkeypatch: pytest.MonkeyPatch) -> None:
    from minicodex.web import routes

    monkeypatch.setattr(routes, "DEFAULT_OWNER", owner)
    monkeypatch.setattr(web_runtime, "DEFAULT_OWNER", owner)


@pytest.fixture
def console(tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch) -> Any:
    _patch_owner(owner, monkeypatch)
    with TestClient(create_app(tmp_path / "console")) as client:
        yield client


def _async_api(tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch) -> httpx2.AsyncClient:
    """An in-process client for the two tests that also run `OAuthStub` --
    a real `asyncio.start_server` -- in the same coroutine.

    `TestClient` answers a request on a *second* thread with its own event
    loop; a test that also `await`s directly on `OAuthStub`'s listening
    socket from the first thread would have to block that thread synchronously
    to get `TestClient`'s answer, which starves the very loop `OAuthStub`
    needs free to accept the connection this console's own outbound request
    is about to make to it. `ASGITransport` calls the app in-process, on
    whichever loop is already running -- one loop, no blocking hand-off, no
    deadlock.
    """
    _patch_owner(owner, monkeypatch)
    app = create_app(tmp_path / "console")
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver")


def _dummy_pending(state: str, *, created_at: float, home: Path) -> PendingOAuth:
    """A `PendingOAuth` with no real network behind it -- for the two tests
    (F22-02, F22-03) that only need the table's own bookkeeping, not a
    completed flow."""
    owner = Owner(home=home)
    context = OAuthContext(
        server_url="https://example.test/mcp",
        client_metadata=OAuthClientMetadata(redirect_uris=["https://console.example/cb"]),
        storage=OwnerTokenStorage(owner, server="gh"),
        redirect_handler=None,
        callback_handler=None,
    )
    return PendingOAuth(
        state=state,
        server_id="srv-1",
        server_name="gh",
        owner=owner,
        redirect_uri="https://console.example/cb",
        code_verifier="a-code-verifier-at-least-forty-three-characters-long",
        context=context,
        created_at=created_at,
    )

```

> - **`owner` / `_patch_owner` / `console`**：每个测试用一个临时目录里的 `Owner`，并把 `routes`、`runtime` 里的 `DEFAULT_OWNER` 换成它——不然测试会在你真正的 `~/.minicodex/` 下面留下一个 `mcp_tokens.json`。
> - **`_async_api`**：说明讲了为什么有的测试不能用 `TestClient`：`TestClient` 在另一个线程的另一个事件循环上回答请求，而假授权服务器在这个线程的循环上监听——一边同步地等另一边，就是死锁。`ASGITransport` 在同一个循环里调用应用。
> - **`_dummy_pending`**：一个不需要网络的 `PendingOAuth`。

F22-01 的两个测试：

```python
async def test_F22_01_begin_and_finish_are_two_independent_calls_a_request_can_sit_between(
    tmp_path: Path,
) -> None:
    """`build_oauth_provider` (chapter 21) blocks one coroutine from
    `redirect_handler` straight through to `callback_handler` returning.
    `begin()` returns as soon as it has a URL; `finish()` is a second call
    with nothing shared except `pending` -- proving a real HTTP request (the
    browser's own redirect back) can land between them, which chapter 21's
    shape cannot allow at all.
    """
    owner = Owner(home=tmp_path / "home")
    async with OAuthStub(supports_dcr=False) as auth:
        config = RemoteServerConfig(
            name="gh", url=auth.resource_url, oauth=RemoteOAuthConfig(client_id="preset-client")
        )
        authorize_url, pending = await begin(
            config,
            server_id="srv-1",
            redirect_uri="https://console.example/api/mcp/oauth/callback",
            owner=owner,
        )
        assert authorize_url.startswith(f"{auth.base}/authorize?")
        params = parse_qs(urlparse(authorize_url).query)
        assert params["code_challenge_method"] == ["S256"]
        assert len(params["code_challenge"][0]) >= 43
        assert params["redirect_uri"] == ["https://console.example/api/mcp/oauth/callback"]
        assert params["state"] == [pending.state]

        # The browser's half of the round trip, driven directly the way
        # chapter 21's `ScriptedBrowser` drives it -- this is the "someone
        # else's tab" this console's callback route stands in for.
        async with httpx2.AsyncClient() as client:
            response = await client.get(authorize_url, follow_redirects=False)
        location = response.headers["location"]
        callback_params = parse_qs(urlparse(location).query)

        # A second, independent call -- exactly what `GET /api/mcp/oauth/
        # callback` does with what the query string handed it.
        await finish(
            pending,
            code=callback_params["code"][0],
            iss=(callback_params.get("iss") or [None])[0],
        )
        assert auth.token_calls == 1

    tokens = await OwnerTokenStorage(owner, server="gh").get_tokens()
    assert tokens is not None and tokens.access_token == "access-1"


async def test_F22_01_starting_oauth_against_a_server_with_no_dcr_and_no_client_id_fails_clearly(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub's own authorization server measured in chapter 21
    (`probe_mcp_remote.py wellknown`): no `registration_endpoint`. Reaching
    that with no preset `client_id` has to fail as a form error naming the
    reason, not a stack trace -- the same F22-04-style translation, for a
    library exception this time (`OAuthRegistrationError`) instead of a
    `ValueError`.
    """
    async with (
        OAuthStub(supports_dcr=False) as auth,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        created = (
            await api.post("/api/mcp", json={"name": "gh", "url": auth.resource_url, "oauth": True})
        ).json()
        started = await api.post("/api/mcp/oauth/start", json={"server_id": created["id"]})
        assert started.status_code == 400
        assert "could not start OAuth" in started.json()["detail"]
```

> - 第一个：`begin()` 和 `finish()` 是两次独立的调用，中间只靠 `pending` 联系——中间那次"浏览器去授权服务器"是一个真的 HTTP 请求。
> - 第二个：没有 `/register`、也没有预置 `client_id`：`begin()` 抛 `OAuthRegistrationError`（GitHub 就是这种）。

变异检查。第一版 12 条，这次改写加了 8 条：

```python
# ---- added when chapter 22 was rewritten --------------------------------
(
    "src/minicodex/web/routes.py",
    "the callback page puts the authorization server's text in as HTML",
    "message=html.escape(message)",
    "message=message",
),
(
    "src/minicodex/web/routes.py",
    "add_mcp accepts a second server with a name already in use",
    '    if any(r.get("name") == body.name for r in _console(request).store.all("mcp.json")):',
    "    if False:",
),
(
    "src/minicodex/web/routes.py",
    "deleting a server leaves its token for the next server with that name",
    '            await OwnerTokenStorage(DEFAULT_OWNER, server=record["name"]).forget()',
```

```
$ uv run python probe_mutations_ch22.py
20 mutations, tests/test_faults_ch22.py

    1 test(s) fail  <-  add_mcp accepts a command and a url together instead of refusing
    1 test(s) fail  <-  add_mcp accepts neither a command nor a url instead of refusing
    1 test(s) fail  <-  add_mcp accepts a bearer token and OAuth together instead of refusing
    1 test(s) fail  <-  a bearer token is echoed back to the browser instead of redacted
    1 test(s) fail  <-  a record's own shape is ignored -- everything dispatches as stdio
    1 test(s) fail  <-  a raw bearer token is never folded into the Authorization header
    2 test(s) fail  <-  a bearer token and OAuth configured together is accepted instead of refused
    2 test(s) fail  <-  an expired pending attempt is never swept -- F22-03's bug
    2 test(s) fail  <-  pop() reads a pending attempt without removing it -- a state becomes reusable
    3 test(s) fail  <-  the callback route trusts an unknown state instead of rejecting it
    1 test(s) fail  <-  resolve_mcp_configs stops checking whether a remote OAuth server has a token
    2 test(s) fail  <-  a server with a token on file is treated as never connected
    1 test(s) fail  <-  the callback page puts the authorization server's text in as HTML
    1 test(s) fail  <-  add_mcp accepts a second server with a name already in use
    1 test(s) fail  <-  deleting a server leaves its token for the next server with that name
    2 test(s) fail  <-  the status route stops saying which token is on file
    1 test(s) fail  <-  a record saved before chapter 22 goes out with no kind
    1 test(s) fail  <-  a token file that cannot be written becomes a 500 in the callback
    2 test(s) fail  <-  begin() ignores the metadata URL the server names in its 401
    1 test(s) fail  <-  one unusable record fails the whole turn instead of being skipped

every mutation was caught.
```

## §14 验证

```bash
uv run ruff format --check . && uv run ruff check .
uv run python scripts/check_layers.py --quiet
uv run pytest -q
uv run python probe_mutations_ch22.py
cd frontend && npm ci && npx tsc --noEmit && npx vitest run && npx vite build
```

这次改写在一台 Linux 机器上（Python 3.13）跑的结果。第 23 步和 `minicodex-live` 也在里面——这一章的修复同样改到了它们：

```
steps/step22_mcp_console   ruff + layers ok   2006 passed, 66 warnings
steps/step22_mcp_console   frontend: tsc ok, vitest 5 passed (5), build ok
steps/step23_multi_user    ruff + layers ok   2064 passed, 132 warnings
steps/step23_multi_user    frontend: tsc ok, vitest 10 passed (10), build ok
minicodex-live             ruff + layers ok   1 failed, 2127 passed, 13 skipped, 176 warnings
    FAILED tests/test_live_review.py::test_M5_a_forged_exit_line_does_not_fool_the_audit[asyncio]
minicodex-live             frontend: tsc ok, vitest 10 passed (10), build ok
steps/step22_mcp_console   probe_mutations_ch19.py   every mutation was caught.
steps/step22_mcp_console   probe_mutations_ch22.py   every mutation was caught.
steps/step23_multi_user    probe_mutations_ch19.py   every mutation was caught.
steps/step23_multi_user    probe_mutations_ch22.py   every mutation was caught.
steps/step23_multi_user    probe_mutations_ch23.py   every mutation was caught.
```

`minicodex-live` 的那一个失败和这一章无关，第 21 章 §15 讲过（一个按 Windows `cmd` 写法写的测试）。第 22、23 步里每个第 19、22、23 章的变异检查都跑了一遍——这一章改了 `routes.py`，那两章的变异也改那个文件。

第一版这一节列了一份"12 条 skip，全部是平台原因"的清单，那是在 Windows 上数的；这台 Linux 机器上 bubblewrap 是装好的，那份清单不适用，这里不再列。

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


### 改写时的提交

```
fix(steps): the console's MCP OAuth escapes what it echoes, forgets what it deletes, and asks the server first

- callback page: html.escape -- error_description and a token
  endpoint's body are the authorization server's text
- one record per name; deleting a remote server forgets its token
- status carries a token fingerprint and start says which one was on
  file, so Reconnect waits for a new token
- begin() reads the server's 401 (resource_metadata, scope) before
  guessing well-known paths
- resolve_mcp_configs reports and skips a record it cannot use
- GET /api/mcp always carries `kind`; OAuthStorageError is a page, not
  a 500; the frontend's clock is injectable; OAuthStartError removed
```

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
