# Ch23 · 谁在用这个控制台：认证、会话、归属与配额

第 20 章写了一个类型，然后在它自己的模块文档里说清楚了它**不是**什么：

> 这是那道**缝**，不是一套多用户系统。这里没有注册、没有会话、没有口令、
> 没有配额，`Owner.key` 被构造它的人完全信任。一个从未认证的请求头里推导出
> owner 的服务器，是把泄漏挪了个地方，不是把它堵上了。

三章过去了，全项目构造 `Owner` 的地方只有一个值：`DEFAULT_OWNER`。而
`serve()` 自己的文档字符串把剩下那句话也说出来了：

> 这个进程替**任何能连上它的人**跑 shell 命令、改文件，唯一的关卡是一个审批
> 弹窗，而它没有任何形式的认证。

这一章就是去构造那个 `Owner`——并且让构造它这件事必须先过一道口令。

---

## 1. 先说清楚这一章没有对照

codex 是一个 CLI 和一个 TUI。它向 ChatGPT **认证自己**，但它从来不替任何人
认证任何人：它没有登录页、没有会话表、没有账号列表、没有配额。

所以这一章**没有 codex 对照可写**，和第 22 章一样。第 22 章那句话在这里
原样成立：说"codex 不做这件事"比在 `codex-rs` 里找一个长得像、但不是同一
回事的文件有用。

这也是用户当初交代这一章时的原话：**"进入系统的认证不一定按 codex 的来。"**
既然不按，就得自己拿主意，并且把拿主意的理由写下来。

---

## 2. 为什么不是"用 GitHub 登录"

第 22 章刚刚把一整套 OAuth 做进控制台。复用它做登录，看上去是最省事的路，
而且省掉了口令这一整摊事。

**不这么做，两个理由，第一个更重要。**

OAuth 2.0 是一个**授权**协议。一次走完的流程给你的是一个能对某个资源动手的
令牌，而"这个令牌的持有者能读某个 GitHub 仓库"和"这个浏览器前面坐的人是
alice"**不是同一个命题**。把前者当后者用，是一类有名字、有案例的漏洞——任何
其他客户端签发的令牌、任何从别的应用里顺出来的令牌，都能登进来。

第二个理由单独也足够决定：第 22 章**量过**，GitHub 的授权服务器不广播
`registration_endpoint`（F21-08b）。所以"用 GitHub 登录"意味着运行这个控制台
的人必须**在第一个人能登录之前**先去 GitHub 注册一个 OAuth App。一个不先去
别人网站上办手续就起不来的自托管工具，不叫自托管。

于是：本地账号、一个口令、一个会话 cookie。

---

## 3. 这一章唯一不手写的东西

这本书的分界线是"教什么就手写什么，不教的就依赖它"。口令哈希不是这一章教的
东西，而且它是整个程序里**最不该自己实现**的一样东西。

`hashlib.scrypt` 就是 RFC 7914，在标准库里。所以既不用加依赖，也不用发明
KDF。这一章自己决定的只有两件事：

**成本参数。** `n = 2**14` 是 RFC 7914 §2 给交互式登录的数字，每次验证要
128 × N × r = 16 MiB 内存——这正是买的东西：一个偷到 `accounts.json` 的人，
每猜一次都要付这笔钱。在这台机器上实测一次 187 毫秒。

**编码格式。** `scrypt$n$r$p$salt$key`，把参数写进哈希本身：

```
scrypt$16384$8$1$oscLYCYra2R033e8stAFaA$...
```

不是装饰。一个不带自己参数的哈希，只有写它的那版代码能校验；带了参数，以后
把成本调高就是改一个常量的事，已经落盘的每一个口令还照样能验。

这个格式还顺手解决了一件测试上的事，第 14 节再说。

---

## 4. 第一个人怎么进来

一个刚起来的、`accounts.json` 还不存在的控制台，怎么产生第一个账号？三条路，
两条是错的，值得把错在哪写清楚：

**默认口令**（`admin`/`admin`）：在每一台"装完没做第二步"的机器上，这是一条
公开的凭证。

**一个不需要认证的"创建第一个账号"路由**：这就是上面那条去掉口令的版本——
谁先摸到端口谁就是运营者。在一台还有别的用户的机器上，那个人不一定是你。

**一个打印到终端的一次性令牌**：把第一个账号绑定给"能看见那个终端的人"，
而那正是决定启动这个服务器的人。Jupyter 先得出这个结论，理由是通用的。

所以 `serve()` 现在会这样：

```
  No account yet. Open the console and use this one-time token:
      3wLq...（24 字节 urlsafe）
  It is not written to disk and a restart issues a new one.
```

`print` 而不是 `log`。日志会去到日志被配置去的任何地方——一个文件、一个
journal、一个日志收集器——而这是一条**应该和那个终端同生共死**的凭证。第 19 章
在监听地址那件事上做了相反的选择，理由也正好相反。

令牌只在内存里，用掉即废。`redeem_bootstrap` 里那行 `self.bootstrap_token =
None` 后面还有故事，第 14 节讲。

---

## 5. F23-01：认证不能是"加上去的"

给一个 FastAPI 应用加认证，最顺手的写法是依赖注入：

```python
def get_thread(thread_id: str, auth: Auth = Depends(require_login)):
    ...
```

**它在每一个写了这个参数的路由上都完全正确。** 问题是没写的那个。

明年有人加一个端点，忘了这个参数，结果是：一个匿名可访问的端点、不抛异常、
不挂测试、code review 必须靠**注意到一个缺席**才能发现。这个控制台现在有
三十多个路由，而在这一章之后写的那些，会由没读过这一章的人写。

所以关卡是一个中间件，它拒绝 `/api` 和 `/ws` 下面的一切，例外是 `auth.py` 里
的一个名单——三个路由，每一个都是关于**如何变成已认证**的：

```python
PUBLIC_PATHS = frozenset({
    "/api/auth/status",
    "/api/auth/login",
    "/api/auth/bootstrap",
})
```

现在"忘了"是**向关的方向失败**的：一个本该公开、但没进名单的新路由，第一个
请求就返回 401——一个会自己报告自己的 bug。

而名单也不能悄悄变长。测试走 `app.routes` 自己那张表：

```python
for route in _every_route(anon.app):
    ...
    assert response.status_code == 401, f"{method} {path} answered {...}"
    checked += 1
assert checked > 20, f"only {checked} guarded routes were found; the walk is not working"
```

实测这张表现在长这样：

```
34 guarded, 3 public
  /api/accounts
  /api/accounts/{key}/disable
  /api/approvals/{approval_id}/respond
  /api/auth/logout
  ...
  /api/workspaces
  /ws/threads/{thread_id}
```

### 最后那行 `checked > 20` 救了这条测试

第一版的走法直接遍历 `app.routes`，一层。跑出来是这样：

```
E       AssertionError: only 0 guarded routes were found; the walk is not working
E       assert 0 > 20
```

`app.routes` 里只有 **6** 项：`/openapi.json`、几个文档页、静态兜底。这个版本
的 FastAPI 把 `include_router` 包成一个 `_IncludedRouter` 对象，真正的三十几
个路由挂在它的 `original_router.routes` 上，一层遍历看不见。

**注意这条测试如果没有最后那行，它会是绿的。** 它会一个路由都没检查、一条
断言都没执行、然后报告成功——而它的名字是
`every_guarded_route_refuses_an_anonymous_caller`。

这是第 19 章 F19-06 换了个方向：那次是 `check_layers.py` 用 `glob` 而不是
`rglob`，于是第 19 章新加的 `web/` 八个模块，一条规则都没覆盖到，而 lint 步骤
在每一个 pull request 上继续打绿勾。当时的修法是让检查器能**看见**代码；这次
的修法是同一句话的另一半——**让检查器在什么都没看见的时候喊出来**。

一条检查里最便宜的一行，往往是"我到底检查了几个东西"。

---

## 6. F23-02：一个真的测量——中间件盖不住 WebSocket

给 Starlette 应用加中间件，教程写法是 `BaseHTTPMiddleware`。

它 dispatch 的是 `scope["type"] == "http"`，其他类型**原样放过去**。

也就是说：一个这样守着的控制台，会认证每一个 REST 调用，然后把
`/ws/threads/{id}` 敞着——那条 socket 上流的是模型的输出、它跑的每一条 shell
命令、以及每一条命令的结果。

这句话是断言还是事实，取决于有没有量过。`probe_ws_auth.py` 就是去量它的：
同样两个路由，两种关卡，不带任何凭证去敲。

```
gate                   GET /api/secret    ws /ws/secret
------------------------------------------------------------------------------
BaseHTTPMiddleware     HTTP 401           OPENED, received 'the socket route ran'
raw ASGI               HTTP 401           refused (WebSocketDisconnect)

Both gates deny every HTTP request. Only one of them is a gate.
```

所以这个关卡是**裸 ASGI** 写的，`http` 和 `websocket` 走同一个判断。

裸 ASGI 的代价也在那个探针里，如实写着：第一版忘了把 `lifespan` 放过去，
应用根本起不来。自己处理每一种 scope 类型，包括那个不是请求的。

**拒绝要发生在 `accept()` 之前。** `websocket.close` 在 accept 之前，对握手
来说是一个 HTTP 403，浏览器的 `WebSocket` 构造函数报的是"连接失败"；先 accept
再 close，则是告诉浏览器"你连上了"——而照着那个行为写的客户端，会去重试一条
它从来就不该拥有的 socket。

---

## 7. F23-03：WebSocket 握手不受 CORS 管

cookie 加上 `SameSite=Lax` 能挡住跨站表单提交。挡不住这个：

**WebSocket 握手不是 fetch，它不受 CORS 约束。** 任何来源的一个页面都可以
`new WebSocket("ws://your-console/ws/threads/...")`，而浏览器会把你的 cookie
附上去。这件事有名字，叫跨站 WebSocket 劫持。

防御是在握手时比对 `Origin` 和 `Host`，而且必须在中间件里——**一个已经 accept
了 socket 的路由已经输了**。

一个**完全没有 `Origin`** 的握手放行。那是脚本、是 `websocat`、是测试，不是
这条检查存在的那个攻击：那个攻击需要一个浏览器去附 cookie，而浏览器在
WebSocket 握手上总是发 `Origin`。

---

## 8. F23-04：多租户主要不是加检查，是把共用的表拆掉

这一章真正的想法在这里。

这个控制台的每一个路由，都是按 id 去找一个东西的：`threads.json` 里的
`thread_id`、`providers.json` 里的 `provider_id`、broker 里的 `approval_id`。
二十二章以来这都是对的，因为每样东西只有一份。

现在要多一个人。直觉的做法是在每个路由里加一句 `if record["owner"] !=
me: raise 403`。

**这是一条你必须记得写、在每一个路由上、永远写下去的行——而忘了写的失败模式
是沉默。** 和 F23-01 是同一个形状。

所以三张让检查变得必要的表，不再是一张表：

| 原来 | 现在 |
|---|---|
| `console.store` | `console.store_for(owner)` → `<data-dir>/tenants/<key>/` |
| `console.broker` | `console.broker_for(account_key)` |
| `channel.emitter(thread_id)` | `channel.emitter(turn_key(account_key, thread_id))` |

之后，"这条 thread 是不是 bob 的"就不是一个谁都能跳过的比较了：

```python
def require_thread(self, owner: Owner, thread_id: str) -> dict[str, Any]:
    record = self.store_for(owner).thread(thread_id)
    if record is None:
        raise HTTPException(404, f"no thread {thread_id!r}")
    return record
```

这里**没有归属检查**，而这不是遗漏。`store_for` 交给这个路由的 store 根在一个
目录上，别人的 thread 不是"存在但被禁止"，而是**不存在**。404 是实话而不是
托词——这也顺带解释了为什么它不泄漏"这个 id 在别处存在"。

磁盘上是这样：

```
accounts.json
tenants/alice/providers.json
tenants/alice/threads.json
tenants/alice/sessions/3852ac0a6319
```

### 那张实在拆不开的表

"在所有设备上退出登录"要求会话表是**一张**表。拆不开的地方，查找就同时吃
两半：

```python
def revoke_id(self, account_key: str, session_id: str) -> bool:
    for key, session in list(self._by_hash.items()):
        if session.id == session_id and session.account_key == account_key:
            ...
```

`session_id` 是浏览器送来的，所以账号**不从它身上取**：调用方传的是它自己
刚认证过的那个账号。少了这一对，"退出这台设备"就是"退出任何人的设备"。

---

## 9. F23-05：四章之前就错的那个目录，和一个一直绿着的测试

做"每个人的记忆分开"的时候，撞上一件和多租户无关的事。

`routes.get_memory` 读的是：

```python
directory = Path(record["workspace"]) / ".minicodex" / "memories"
```

`runtime.load_feature_dirs`（也就是**回合真正读记忆的地方**）读的是：

```python
loaded = load_memory(DEFAULT_MEMORY_DIR)   # ~/.minicodex/memories
```

**这两个不是同一个目录，从第 19 章起就不是。** 第 16 章把记忆从仓库挪到了
home，控制台的面板没跟上。所以：一台记忆文件写得满满的机器，那个标签页是空
的；"忘掉全部"删的是一个没人写过的目录。两件事都不报错。

`step19`/`step20`/`step21`/`step22` 四个 step，同一行。

### 更值得记的是为什么没人发现

第 19 章有一条测试，编号 F19-11，名字叫
`everything_it_learned_can_be_erased_from_where_it_was_enabled`：

```python
memories = workspace / ".minicodex" / "memories"
memories.mkdir(parents=True)
(memories / "MEMORY.md").write_text(...)
...
assert "MEMORY.md" in response.json()["removed"]
```

**它是绿的。** 因为它的路径是从它要测的那段代码里抄来的。

这次改完之后它红了，红成这样：

```
>       assert "MEMORY.md" in response.json()["removed"]
E       AssertionError: assert 'MEMORY.md' in []
```

一条回归测试，只可能和它继承的那个假设一样正确。它测的是"代码和它自己一致"，
不是"代码和它承诺的事一致"——而这两件事的区别，正好是这条测试的名字。

### 还有更扎心的一层

往下两个函数，是 skills 面板：

```python
directory = Path(record["workspace"]) / ".minicodex" / "skills"
```

**这一个是对的。** 因为第 16 章把记忆挪走的时候，skills 留在了 per-project。

两个面板挨着写、形状一模一样，只有一个是对的。**隔壁那个长得一样，不构成
证据。**

而第 20 章为 `load_feature_dirs` 的"记忆是全局的"专门写了一整节（F20-08），
盯着这个函数看了一章，没有回头看它隔壁那个面板在读哪儿。

修法是两边都写 `owner.memories()`，并且 `run_turn` 收同一个 `Owner`——让它们
**因为构造而一致**，不是因为碰巧在同一天被改过。

---

## 10. F23-06：145 毫秒对 0.148 毫秒

`AccountStore.verify` 最自然的写法：

```python
account = self.get(key)
if account is None:
    return None
if not verify_password(password, account.password_hash):
    return None
```

`verify_password` 里面老老实实用了 `hmac.compare_digest`。教科书上的那一条
做到了。

**然后整个东西还是漏的。** 因为 `if account is None: return None` 在哈希
**前面**：账号不存在的时候，这个函数在微秒级返回；账号存在的时候，它要付
scrypt 的钱。

于是"失败要多久"就把账号列表枚举出来了。而在这个控制台里这件事比听上去更糟：
一个账号 key 同时是一个目录名和一个租户。

量一下（这台机器，五次平均）：

```
               known account   no such account
without the dummy hash   145.3 ms      0.148 ms
as shipped               142.6 ms    178.548 ms
```

**差了一千倍。** 隔着网络也看得见。

修法是：查不到的时候，拿口令去校验一个谁也不知道的哈希，然后把结果扔掉。

```python
account = self.get(key.strip().lower())
if account is None:
    verify_password(password, _dummy_hash())
    return None
```

现在每一次登录都花一次 scrypt——而这正是要买的那个性质。

路由那一层跟着来：账号不存在、口令不对、账号被停用，**同一个 401、同一句话**。
说"没有这个账号"会把上面那笔钱白花。

---

## 11. F23-07：撤销了访问，没有停下工作

一个回合是一个**活过了发起它的那个请求**的后台任务。

所以"在所有设备上退出登录"如果只清空会话表，结果是：agent 还在替一个**一条
有效凭证都不剩**的账号改文件、跑命令——而且没有人在看那条 socket，这让它更糟
而不是更好。

```python
def _revoke_everything(console: Any, account_key: str) -> dict[str, int]:
    sessions = console.sessions.revoke_account(account_key)
    prefix = f"{account_key}/"
    cancelled = 0
    for key, task in list(console.running.items()):
        if key.startswith(prefix) and not task.done():
            task.cancel()
            cancelled += 1
    console.broker_for(account_key).fail_all("signed out")
    return {"sessions": sessions, "turns": cancelled}
```

退出全部、改口令、停用账号，三条路都调它。

`task.cancel()` 是停止按钮调的同一个调用，所以回合按第 7 章承诺的方式结束：
`mark("interrupted")`，一个可恢复的会话文件。

**改口令这件事也是同一个道理。** 一次不把旧会话赶出去的改口令，没有赶走它
本来要赶走的那个人。所以：撤销全部，给发起请求的这个浏览器发一个新会话，其他
每一台设备拿新口令重新登。

---

## 12. F23-08：配额在**认领**时收费

在完成时收费，看上去更公平，其实是错的：**一个失败的回合花掉的模型调用、
子进程和秒数，和成功的一模一样。** 只算成功的配额，是一个重试循环不需要遵守
的配额。

所以收费点在路由里那一段同步代码里，紧挨着第 19 章 F19-01 已经放在那儿的
`busy` 认领：

```python
try:
    console.quota.claim(
        auth.account.key,
        auth.account.quota_limits(),
        running=console.running_for(auth.account.key),
    )
except QuotaExceeded as exc:
    raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)}) from exc
console.busy.add(key)
```

`QuotaLedger` 上**没有** `complete()` 方法，而且有一条测试断言它没有：

```python
assert not hasattr(ledger, "complete")
```

唯一的退款，是给一个**从来没开始**的回合：没配模型 provider 的 400。那个什么
都没花。

### 为什么是回合数而不是 token 数

托管产品要的是 token 或者钱。**这个程序没有那个数。**

`model.py` 确实解析了服务器报回来的 `prompt_tokens` / `completion_tokens`
（第 6 章 F06-07 为此专门开了 `stream_options.include_usage`）。但唯一消费它
们的是 `tokens.Calibration`，而它保留的是一个**比值**，把总数扔掉了。

拿 `Calibration.correct()` 去给人计量——一个自己的文档里写着"对一个会话的前
两回合毫无用处"的估计值——会得到一个有小数点、但辩护不了的数字。

回合数是精确的，在唯一能执行的那个时刻是已知的，而且不需要把一个计费的关切
往下推进 agent。**这是范围决定，不是疏忽**，所以它写在 `quota.py` 的模块文档
里而不是这里。

---

## 13. F23-09：workspace 是这一章唯一没被 key 罩住的东西

这一章分开的每一样东西，都住在一个 key 底下：记录、token、记忆、审批。

**workspace 不是。** 它是服务器上的任意一个绝对路径，而被指向它的那个东西，
整个存在的意义就是在那儿读文件、跑命令。

两个 workspace 不受限的账号，就是两个可以各自在对方 home 目录上开 thread 的
账号——底下的租户切得再干净也没用。

```python
resolved = path.resolve()
roots = getattr(account, "workspace_roots", ()) or ()
if roots and not any(_within(resolved, Path(root)) for root in roots):
    raise HTTPException(403, f"{resolved} is outside the directories this account may use: ...")
```

`_within` 用 `Path.is_relative_to`，**不是字符串前缀**——前缀会对根目录
`/srv/data` 放行 `/srv/data-of-someone-else`。两边都先 `resolve()`，所以一个
指向根外面的符号链接是在比较**之前**被跟随的，不是之后。第 20 章 F20-06 在
沙箱的可写根上学过同一件事，这是它上移了一层。

**空的 `workspace_roots` 仍然表示不受限**，因为那正是一个单人安装的样子，而
这个控制台通常仍然是那个。它在创建第二个账号的时候变成一个决定——所以创建
表单问它，账号页显示它，而不是把它藏进一个配置文件。

---

## 14. F23-10：第 22 章写了四个前端测试，CI 从来没跑过

给这一章接 CI 的时候，先是这条测试红了：

```
E       assert not ['probe_mutations_ch23.py']
tests\test_packaging.py:209: AssertionError
```

`test_F_1_05_every_mutation_script_runs_somewhere`——第 12 章加的，因为第 11 章
的十九条变异脚本从来没被接进 workflow。它在这一章的脚本写完一分钟之内就抓住
了我。

然后顺手去看前端那边有没有同样的东西。

```
$ grep -rn "npm" .github/workflows/ | grep -v "npm ci"
ci.yml:66:          npm --prefix frontend run typecheck
ci.yml:78:          npm --prefix frontend run build
```

**没有 `npm test`。** 第 22 章写了 4 个 vitest 测试，它们是对的，在笔记本上是
绿的，而在那一章存在的整段时间里，**没有一个 pull request 执行过它们**。

这是"写在散文里的承诺不是机制"这条老规矩，换了一门语言。Python 那边从第 12 章
起就有机制；TypeScript 那边有散文，没有机制。

修法是 `ci.yml` 里一行，加上一条断言那一行存在的测试。断言的是**runner 被调用
了**，不是逐个列文件——`vitest run` 自己会发现 `**/*.test.ts`，在这里列一份
清单，就是把会过期的那个东西重新造一遍。

---

## 14.5 F23-11：那条机制自己也是散文

上面那条"Python 那边有机制"的话，写完之后没多久就要打折扣。

这一章收尾时去清一个旧尾巴：`probe_mutations.py`（第 5 章那份通用脚本）在
`step12_retry` 里没接进 CI，所以那个 step 一直带着一条红测试。修法很简单——
补一行。补完顺手想验证一件事：**在没补之前，那条断言到底会不会红？**

于是在一个闲着的 `step13_system_prompt` 里，把那一行 `run:` 删掉，跑测试，
**期待它变红**。

它是绿的。

原因在那条测试自己身上：

```python
workflow = (repo_root / ".github/workflows/postmerge.yml").read_text(encoding="utf-8")
missing = [name for name in scripts if name not in workflow]
```

`read_text()` 读的是**整个文件的文本**，而 YAML 注释也是文本。所以一份在注释里
提到 `probe_mutations.py`、但一行都不跑它的 workflow，照样通过。

而这不是理论上的：**第 13 章那次修复，就在它新加的那个步骤正上方写了一段注释，
里面写着这个文件名**——从第 13 章往后每一个 step 都是这样。删掉那个步骤、留下
注释，这条测试还是绿的。

实测（把 `run:` 那一行从文本里去掉，再用两种方式问同一个问题）：

```
CI no longer runs probe_mutations.py.
what test_F_1_05 reports: PASS (missing=[])
what a `run:`-only check reports: FAIL ['probe_mutations.py']
```

**这是那条"散文里的承诺不是机制"的检查，被散文满足了。**

修法是不再问文件，而是问**真正会执行的那些命令**：

```python
def ci_commands(workflow: Path) -> str:
    parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return "\n".join(
        step["run"]
        for job in parsed["jobs"].values()
        for step in job["steps"]
        if isinstance(step.get("run"), str)
    )
```

回归测试写在一份**合成的** workflow 上，不是去改真的那一份——一个为了证明观点
而改动仓库的测试，就是一个可能把仓库改坏了留在那儿的测试。这句话是
`probe_mutations.py` 自己的 docstring 说的，而它是踩过之后才写下的。

### 这一条是怎么被发现的，值得记

不是靠读代码读出来的。是**做了一个实验、结果和预期相反**：我删掉一行、期待
测试变红，它没有。

如果当时只是"补上那一行、看到测试变绿、收工"，这个洞会原样留着——而且会一直
留在一条**名字里写着它在防这件事**的测试里。

顺带一提，我最初的假设（"探针的 baseline 检查会拦住它，所以它在修好之前根本
跑不起来"）也是错的：`baseline: green` 就打印在那次实验的输出里。两个预期
错了两次，才换来这一条。

---

## 15. 那些不算故障的决定

有几个决定值得写下来，但它们从来没坏过，所以不给编号（完整表格在
[`FAULTS.md`](../FAULTS.md) 里）：

**会话活在进程里，不落盘。** 重启把所有人签出，这是"一个你自己启动的工具"
的行为，不是它的限制。落盘要多一个几乎每个请求都要写的凭证文件（空闲时钟在
请求到达时会动），而且它会活得比它指向的东西更久——一个熬过重启的会话，指向的
是已经不存在的审批和已经不在跑的回合。

**但 token 在那张内存表里仍然存哈希。** 看着像迷信，直到你问那张表会在哪里
被渲染：`GET /api/auth/sessions` 要列出来，好让"退出全部设备"这件事存在。一张
存着原文的表，离"把活凭证发出去"只差一次不小心的序列化、一个 traceback 里的
`repr()`、一行调试日志。存了哈希，那个错误就不在选项里了。

用的是一次 SHA-256 而不是 scrypt，这个区别正好是 `accounts.py` 用 scrypt 的
理由：口令是低熵的、可猜的，所以验证它必须故意做贵；会话 token 是 `secrets`
给的 32 个随机字节，没有什么可猜，而且这段代码**每个请求都要跑一次**。

**cookie 的 `Secure` 是有条件的。** 无条件加上，浏览器会在
`http://localhost:8000` 上直接把它丢掉——而那正是这个控制台通常的跑法，于是
登录看起来成功了然后不工作。`HttpOnly` 和 `SameSite=Lax` 是无条件的；`Lax`
就是这个控制台的 CSRF 防御，而且在每一个改状态的路由都是 POST/PATCH/DELETE
的前提下它够用。

**第 22 章以前的记录，在 bootstrap 时被第一个账号收养一次。** 四章的控制台
都把 `threads.json` 写在数据目录顶上。留着不动，运营者第一次登录看到的是一个
空控制台，而他所有的会话还在上一级目录里——不管是不是数据丢失，读起来就是。
只在 bootstrap 路由里做一次，绝不在每次启动时做：一个见到散落文件就收养的
控制台，会把上一个账号留下的东西交给**下一个**账号。它是 rename，遇到已存在
的目标就停，因为这里没有任何东西知道怎么合并两个 `threads.json`，而假装知道
正是其中一个被悄悄弄丢的方式。

**账号只停用，不删除。** 一个账号 key 是两棵目录树下的目录名，其中一棵放着
别人服务器的 OAuth token。删掉那一行而目录还在，等于允许同一个 key 被再发一
次并且继承它们。

---

## 16. 一个真的等价变异，说清楚而不是藏在绿勾里

变异探针跑完，有一条是这样的：

```
1 test(s) fail  <-  the password comparison stops at the first differing byte
```

它现在被抓住了，但**不是被一条行为测试抓住的**。

`hmac.compare_digest(a, b)` 和 `a == b` 对每一个输入都返回同一个值。没有任何
行为测试能分开它们，而给一个 32 字节的比较计时，在 CPython 里量到的是解释器。

这个性质是真的，而它是**文本的**性质。所以测试去读那一行：

```python
source = (repo_root / "src" / "minicodex" / "web" / "accounts.py").read_text(...)
assert "hmac.compare_digest(computed, expected)" in source
assert "computed == expected" not in source
```

第 19 章对一个前端确认对话框已经这么做过（F19-11）。形状一样，**局限也一样**：
一条静态断言只能告诉你某个函数被调用了，不能告诉你它做的事和它的名字一致。

写下来比让它藏在绿勾后面好。

---

## 17. 清点

| 编号 | 一句话 | 怎么发现的 |
|---|---|---|
| F23-01 | 依赖注入式的认证是 opt-in 的，忘了写就是开着 | 🟣 |
| F23-02 | `BaseHTTPMiddleware` 盖不住 WebSocket | 🟢 |
| F23-03 | WebSocket 握手不受 CORS 管，需要 Origin 检查 | 🟣 |
| F23-04 | 每个路由按 id 查，从来没问过是谁的 | 🟣 |
| F23-05 | 记忆面板读的目录，没有任何回合用过 | 🟡 |
| F23-06 | 登录失败的耗时把账号是否存在说出去了 | 🟣 |
| F23-07 | 撤销了访问，正在跑的回合还在跑 | 🔵 |
| F23-08 | 按完成计费的配额，重试循环不用遵守 | 🔵 |
| F23-09 | workspace 是任意绝对路径，租户在这里漏掉 | 🟣 |
| F23-10 | 第 22 章的四个前端测试，CI 从来没跑过 | ⚪ |
| F23-11 | 而那条管 CI 接线的检查，一句注释就能满足它 | 🟢 |

全书 **245** 条。

---

## 18. 验证

```
$ uv run ruff check .
All checks passed!

$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone

$ uv run pytest
1927 passed, 12 skipped in 181.84s (0:03:01)

$ uv run python probe_mutations_ch23.py
28 mutations, tests/test_faults_ch23.py tests/test_packaging.py
...
every mutation was caught.

$ npm --prefix frontend test
 ✓ src/auth.test.ts (5 tests) 5ms
 ✓ src/mcpOauth.test.ts (4 tests) 28ms
 Test Files  2 passed (2)
      Tests  9 passed (9)

$ npm --prefix frontend run typecheck && npm --prefix frontend run build
✓ 39 modules transformed.
✓ built in 707ms
```

本章自己 **57 个测试，一条都不跳**（F23-11 那条在 `test_packaging.py` 里，
和它修的那条检查放在一起，所以本章一共 58 条）。

12 条跳过全是平台原因，和这一章无关：

```
7  tests/test_shell.py       F02-10: POSIX 进程组和 POSIX shell 内建
1  tests/test_faults_ch07.py F02-10: killpg 是 POSIX-only
1  tests/test_approval.py    这个平台上建符号链接要权限
2  tests/test_faults_ch20.py 这台机器上没有 bwrap
1  tests/test_faults_ch21.py POSIX 文件权限位；Windows 用 ACL
```

### 变异探针先找出了四条测试没写

第一遍跑（当时 27 条，F23-11 是后来补的）活下来 4 条。它们各自的处理值得分开
说，因为**"活下来"有四种不同的原因**：

| 活下来的变异 | 真相 | 做法 |
|---|---|---|
| 停用的账号还能用它的会话 | 停用路由自己会撤销会话，所以 `resolve` 里那句 `disabled` 检查从来没被执行过 | 补一条测试，**绕过路由**直接改 `accounts.json`——那才是这句检查存在的场景 |
| 回合读的是默认 owner 的记忆 | 上面每一条测试都走路由，测的是**面板**；`load_feature_dirs` 本身没人直接测 | 补一条直接调它的测试。面板对、回合错，正好是这一章在修的那个 bug 往里挪一个函数 |
| 常量时间比较退化成 `==` | 真等价变异 | 见第 16 节，改成源码断言并写明局限 |
| 用过的 bootstrap token 没被清掉 | 第二次 redeem 会被 `self.empty()` 拦住，所以清空那一行的**行为**没人测 | 断言那个属性本身变成了 `None`——一条花掉的凭证不该还坐在进程内存里 |

四条里有三条的形状是一样的：**那行代码有另一条路径掩护着它**，所以改坏它
不改变任何测试的结果。停用被路由自己的撤销掩护，token 清空被 `self.empty()`
掩护，`load_feature_dirs` 被面板掩护。掩护不是冗余——它们各自都有独立成立的
理由——但它意味着那一行**没有被断言过**，而变异测试是唯一会说出这件事的东西。

顺带记一笔：第 6、14、16、17、18、20、21 章各有一次是**测量工具自己出了问题**
（错的不是数字，是关于别的东西的数字）。这一次不是——探针老老实实工作，出问题
的是我写的测试。这两种情况长得很像，区别在于跑完之后该改哪一边。

---

## 19. 收工

这一章做完之后，第 20 章那段自我限定的话终于可以改了：

```python
# tenancy.py
They arrived.  `web/accounts.py` is the thing that constructs an `Owner`, and
`web/auth.py` is what makes constructing one require a password first.  Nothing
in this file changed to make that possible, which was the point of writing it
three chapters early.
```

`tenancy.py` **一行没改**。那是提前三章写下那道缝的全部意义。

### 三件事

**一、能忘的检查不算机制。** 这一章有两处用了同一招：认证不是每个路由上的
一个参数（F23-01），归属不是每个路由上的一句 `if`（F23-04）。两次都是把
"必须记得做的事"换成"做不到的事"——一个不在名单上的路由**返回 401**，一张不
包含别人记录的表**没法被索引到**。剩下真拆不开的（会话表），查找就吃一对。

**二、一条回归测试只能和它继承的假设一样正确。** F19-11 绿了四章，因为它的
路径是从它要测的代码里抄来的。而两个函数之外，同样形状的 skills 面板是对的
——**隔壁那个长得一样，不构成证据**。

**三、你已经有的机制，去看看它在别的语言那边有没有。** `test_F_1_05_every_
mutation_script_runs_somewhere` 从第 12 章起就在，一分钟内抓住了我漏接的
workflow。同一个问题在前端那边存在了一整章，因为那边只有散文。

### 留给下一个人的

**这个控制台的多用户是能用的，但它不是一个托管产品**，而这两件事的距离值得
如实写出来：

- **没有 TLS 终止、没有速率限制的登录节流。** 口令哈希贵到能挡住离线爆破，
  挡不住一个在线的、每秒一次的慢速尝试。真要放到公网上，前面还得有东西。
- **没有 token/费用配额**，理由在第 12 节，也写着要改的话得改什么。
- **审计日志没有。** 谁在什么时候登录了、开了哪个 workspace、跑了什么命令——
  rollout 文件里有第三样，前两样没有。
- **`bind 0.0.0.0` 仍然只是一个警告。** 有了登录之后仍然默认 `127.0.0.1`，
  因为这是两个不同的问题：登录决定**谁**可以驱动 agent，绑定地址决定谁能
  **够到这个端口**。有了后者就撤掉前者，是拿一条边界换一条凭证。

---

## 20. 练习

1. **把 F23-05 那条 bug 装回去**：把 `routes.get_memory` 里的
   `auth.owner.memories()` 换回 `Path(record["workspace"]) / ".minicodex" /
   "memories"`，跑 `tests/test_faults_ch23.py`。数一下几条红。然后想一想：
   为什么第 19 章的那条测试当时**没有**红。

2. **把关卡换成 `BaseHTTPMiddleware`**，跑 `probe_ws_auth.py`，再跑
   `tests/test_faults_ch23.py`。哪一条测试抓住了它？如果那条测试不存在，
   什么会告诉你？

3. **给配额加上 token 计量。** 提示：`model.py` 已经有
   `Usage(prompt_tokens, completion_tokens)`，缺的是一个跑总数。想清楚它该
   累在哪一层——`Agent`、`RunResult`，还是 `web/runtime.py`——以及
   `scripts/check_layers.py` 对你的答案有没有意见。

4. **写一条"登录节流"**：同一个账号 key 连续失败 N 次之后，退避 M 秒。做完
   问自己两个问题：它记在哪儿（内存？重启就清零，可以吗），以及它会不会变成
   一个**拒绝服务**——攻击者可以靠故意猜错来把真正的用户锁在门外。这个权衡
   没有免费的答案，写下你选了哪边。

5. **审计日志。** 从最小的一条开始：登录成功、登录失败、创建账号、停用账号。
   然后问一个这本书反复问过的问题——**这条日志本身会不会变成泄漏**（比如把
   口令、token 或者别人的账号名写进去）？
