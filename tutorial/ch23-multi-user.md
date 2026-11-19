# 第 23 章 · 谁在用这个控制台：认证、会话、归属与配额

> **代码**：`steps/step23_multi_user/`
> **分支**：`feat/multi-user`
> **产出**：`src/minicodex/web/accounts.py`（账号、口令哈希、第一个账号怎么来）、`web/sessions.py`（会话表）、`web/auth.py`（关卡）、`web/quota.py`（配额）、`app.py`/`routes.py`/`runtime.py`/`store.py`/`channel.py` 按账号拆开、前端的登录页和账号页、`probe_ws_auth.py`、`probe_mutations_ch23.py` 32 条
> **前置**：做完第 22 章。本章 62 个后端测试、5 个前端测试，全部离线
> **这是全书最后一章。**

---

## 0. 开工前的准备

### 0.1 本章会遇到的新概念

- **认证（authentication）**：确认"你是谁"。这一章用最朴素的方式：账号名加口令。
- **授权（authorization）**：确认"你能做什么"。第 21、22 章的 OAuth 是**授权**协议——§2 讲为什么不能拿它来做认证。
- **口令哈希 / KDF**：不存口令本身，只存一个"从口令算出来、但算不回去"的值。**scrypt** 是一种**故意算得很慢、很费内存**的哈希，让偷到文件的人每猜一次都付出代价（§3）。
- **盐（salt）**：每个口令配一段随机字节一起哈希，两个人用同一个口令，存下来的值也不同。
- **会话（session）/ cookie**：登录成功后，服务器发给浏览器一串随机的**令牌**，浏览器之后每个请求都在 cookie 里带着它，服务器靠它认出"还是你"。
- **中间件（middleware）**：包在整个应用外面的一层，**每一个**请求都先经过它。这一章的关卡就是一个中间件（§5）。
- **ASGI**：Python 异步 Web 应用的接口规范。FastAPI 底下是 Starlette，Starlette 底下是 ASGI：一个请求就是一个 `scope` 字典加两个函数 `receive`/`send`。
- **同源（same origin）/ 同站（same site）**：两个网址的"协议 + 主机 + 端口"都相同才算**同源**；**同站**宽松得多——`http://localhost:3000` 和 `http://localhost:8000` 端口不同、不同源，却是**同站**（§7.1 的关键）。
- **CORS**：浏览器的一套规则，决定一个页面能不能**读到**另一个源的响应。注意它管的是"读"，不总是管"发"。
- **CSRF（跨站请求伪造）**：别的网页让你的浏览器带着你的 cookie 去请求你登录过的网站。
- **`SameSite` cookie 属性**：`Lax` 表示跨**站**的 POST 不带这个 cookie——它挡的是跨站，不是跨源。
- **`Origin` 请求头**：浏览器在 POST 和 WebSocket 握手里告诉服务器"这个请求是哪个页面发起的"。
- **时序攻击（timing attack）**：从"服务器回答得多快"推断出秘密（§10）。
- **多租户（multi-tenant）**：一个服务器给好几个人用。第 20 章写了 `Owner` 这道缝，这一章第一次真的有了第二个人。
- **配额（quota）**：每个人能用多少（§12）。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `hashlib.scrypt(pw, salt=..., n=..., r=..., p=..., dklen=...)` | 标准库里的 scrypt（RFC 7914） |
| `hmac.compare_digest(a, b)` | 比较两个字节串，花的时间不取决于它们在哪一位开始不同 |
| `secrets.token_urlsafe(32)` / `secrets.token_bytes(16)` | 密码学安全的随机数 |
| `base64.urlsafe_b64encode(raw).rstrip(b"=")` | 编码成可以放进网址的文本，去掉末尾的填充 |
| `@dataclass(frozen=True)` + `dataclasses.replace(obj, field=...)` | 不可变对象；要改，就造一个改了一个字段的新对象（§18 有一个这样的 bug） |
| `collections.deque` / `popleft()` | 两端都能快速进出的队列，配额的滑动窗口用它 |
| `async def __call__(self, scope, receive, send)` | 一个类实现了 `__call__`，它的实例就能当函数调用——这就是一个 ASGI 应用/中间件 |
| `urlsplit(url).netloc` / `.hostname` / `.scheme` | 拆网址 |
| `Path.is_relative_to(other)` | `self` 是不是在 `other` 下面（按路径，不是按字符串前缀） |
| `os.open(path, flags, 0o600)` / `os.fdopen(fd, "w")` | 创建文件时就定好权限（第 21 章 §8.2 见过） |
| `response.set_cookie(name, value, httponly=True, samesite="lax", secure=...)` | FastAPI：设一个 cookie |
| `fastapi.testclient.TestClient(app)` **作为** `with` 使用 | 测试客户端；**不用 `with`** 时每个请求跑在一个用完就关的事件循环上（§18） |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/multi-user
```

### 0.4 这次改写时找到的

第一版的 57 个测试全部是绿的，变异检查 28 条全部抓住。改写时做了几件第一版没做的事：用**一个非管理员账号**真的走一遍"它能对这台服务器做什么"；在 Linux 上重新量每一个数字；把测试里的每一轮对话**等到它真的调用了模型**。找到了这些：

1. **任何一个账号都能拿到运营者的 OpenAI key**：一个账号添加一个没填 key 的 `openai` 模型来源、把 `base_url` 指向自己的服务器——第一版会把服务器环境变量里的 `OPENAI_API_KEY` 填进去，发给那个地址。实测，非管理员账号收到了 `Bearer sk-...`（§13.2）。
2. **这一章的"多租户"不是一条安全边界，第一版没有说**：每个账号都能让 agent 以**服务器用户的身份**运行命令（`run_shell` 的审批由这个账号自己点；一个 stdio MCP 服务器的命令由这个账号自己写）。实测：agent 的 shell 读 `/proc/$PPID/environ` 就拿到了运营者的 key。别人的记录、token、口令哈希，都在同一个用户能读的磁盘上（§13.1）。
3. **同站的页面可以对控制台发 POST**：`SameSite=Lax` 不看端口，`localhost:3000` 上的页面发给 `localhost:8000` 的 POST 带着 cookie；一个没有正文的 POST 不需要 CORS 预检。第一版只在 WebSocket 上检查 `Origin`——这样的页面可以停用账号、把所有人签出、取消正在跑的回合（§7.1）。
4. **`accounts.json` 和每个账号的 `providers.json` 是 0644**：口令哈希和 API key，这台机器上每个用户都能读（§3.1）。
5. **`minicodex-live` 的控制台一轮对话都跑不起来**：给 shell 加审计时对一个冻结的 dataclass 赋值，`FrozenInstanceError`，在调用模型之前。没有测试让一轮对话走到模型那一步（§18）。
6. 测试里的一个陷阱：不用 `with` 的 `TestClient`，后台的回合会随着请求结束一起消失——这次改写自己写的第一个测试就掉进去了，在没修的代码上也是绿的（§18）。
7. 几处正文和说明的错误：数字是 Windows 上量的（这次在 Linux 上重量了）；"全书 245 条"（是 259）；"`tenancy.py` 一行没改"（代码没改，说明加了一段）；`adopt_legacy_records` 说"遇到已存在的目标就停"（其实是跳过那一个、继续搬其余的）；"第 22 章量过"（是第 21 章）。

还有一件不是 bug、但第一版没说的事：从单人控制台升级上来时，被收养的只有**数据目录**里的记录。`~/.minicodex/memories` 和 `~/.minicodex/mcp_tokens.json`（第 16、21 章，单人时的位置）不会搬进第一个账号的 `tenants/<key>/` 下——升级后控制台里的记忆是空的，远程 MCP 服务器显示"需要连接"。命令行还在用原来的位置，所以这一章没有去搬它们（§15）。

---

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

第二个理由单独也足够决定：第 21 章**量过**，GitHub 的授权服务器不广播
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
每猜一次都要付这笔钱。第一版在它的 Windows 机器上实测一次 187 毫秒；这次改写的 Linux 机器上大约 34 毫秒（§10）。

**编码格式。** `scrypt$n$r$p$salt$key`，把参数写进哈希本身：

```
scrypt$16384$8$1$oscLYCYra2R033e8stAFaA$...
```

不是装饰。一个不带自己参数的哈希，只有写它的那版代码能校验；带了参数，以后
把成本调高就是改一个常量的事，已经落盘的每一个口令还照样能验。

这个格式还顺手解决了一件测试上的事，第 14 节再说。

### 3.1 代码：`accounts.py` 的前半——口令

模块开头的说明讲的就是上面 §1–§3；然后是几个常量和两个函数：

```python
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicodex.tenancy import Owner, OwnerError

from .quota import Limits
from .store import open_private

# scrypt cost. 2**14 is the interactive-login figure from RFC 7914 §2 and
# costs 128 * N * r = 16 MiB of memory per verification, which is the point --
# an attacker with a stolen `accounts.json` pays that per guess.
#
# Written into every stored hash rather than only read from here, so raising
# them later does not invalidate the passwords already on disk: `verify` takes
# the cost from the string it is checking, and only `hash_password` reads these
# names. That is the difference between a parameter and a constant.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

#: The shortest password this console accepts. Length only -- no character
#: classes, which push people towards `Passw0rd!` and are worth less than four
#: more characters. NIST SP 800-63B dropped composition rules for the same
#: reason.
MIN_PASSWORD_LENGTH = 10

#: Format: `scrypt$<n>$<r>$<p>$<salt-b64>$<key-b64>`. Self-describing on
#: purpose: a hash that does not carry its own parameters is a hash that can
#: only be checked by the version of the code that wrote it.
_ENCODING = "scrypt"
```

> - **`SCRYPT_N/R/P`**：scrypt 的三个成本参数。注释讲了为什么它们会被**写进**每一个哈希（下面），而不是只在这里读。
> - **`MIN_PASSWORD_LENGTH = 10`**：只看长度，不要求"大写+数字+符号"（NIST SP 800-63B 已经去掉了这类要求）。
> - **`from .store import open_private`**：§3.2 加的。

```python
class AccountError(ValueError):
    """A registration or a login this module refuses."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, n: int | None = None) -> str:
    # `None` and a lookup rather than `n: int = SCRYPT_N`, because a default
    # argument is evaluated once at import and would freeze the cost into this
    # function. Read here, the module attribute is still the single place the
    # figure lives -- and raising it is a one-line change that takes effect on
    # the next password written, with every existing hash still verifiable
    # because it carries the cost it was written with.
    n = SCRYPT_N if n is None else n
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"a password needs at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES
    )
    return f"{_ENCODING}${n}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash, in constant time.

    `hmac.compare_digest` rather than `==` because the second one stops at the
    first differing byte, and the number of bytes it got through is a fact
    about the secret that a caller can time. That is textbook, and it is also
    not the interesting comparison in this file -- see `AccountStore.verify`,
    where the leak is *whether the account exists*, which no amount of
    constant-time byte comparison down here can hide.
    """
    try:
        kind, n, r, p, salt, key = encoded.split("$")
        if kind != _ENCODING:
            return False
        expected = _unb64(key)
        computed = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        # A malformed or unrecognised hash is a failed login, not a crash. One
        # unparseable line in `accounts.json` must not be able to take the
        # login route down for everybody else.
        return False
    return hmac.compare_digest(computed, expected)


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_urlsafe(32)))
    return _DUMMY[0]
```

> - **`hash_password`**：太短就拒绝；16 字节随机盐；`hashlib.scrypt` 算出 32 字节；拼成 `scrypt$n$r$p$盐$结果`。`n` 用 `None` 做默认值再在函数里读模块常量——注释讲了为什么（默认参数在 import 时就定死了）。
> - **`verify_password`**：把存的字符串拆开，**用它自己记着的参数**重算一遍，`hmac.compare_digest` 比较。格式不对、解不开，都算"不对"，不崩（§16）。
> - **`_dummy_hash`**：一个谁也不知道口令的哈希，第一次用到时才算（它要跑一次完整的 scrypt），§10 用它。

账号记录和它的 JSON 形状：

```python
RESERVED_KEYS = frozenset({"tenants", "sessions", "accounts", "admin", "api", "ws"})


_HANDLE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class Account:
    """One person who may sign in, and the tenancy they get when they do."""

    key: str
    password_hash: str
    created: float
    #: The first account, and anyone it promotes. An admin may list accounts,
    #: create them and disable them; it is deliberately *not* "can read
    #: everyone's threads", because there is no route that does that -- an
    #: escalation this console cannot perform is better than one it can perform
    #: for the right people.
    admin: bool = False
    disabled: bool = False
    #: Absolute paths this account may open a workspace under. Empty means
    #: unrestricted, which is right for the single-operator install this
    #: console started as and wrong the moment there are two accounts -- see
    #: `routes._validated_workspace`.
    workspace_roots: tuple[str, ...] = ()
    #: Overrides for `quota.Limits`, empty for the defaults. A tuple of pairs
    #: rather than a `Limits` because it is a slice of a JSON file, and because
    #: "this account said nothing about `turns_per_hour`" has to keep meaning
    #: "whatever the default is now" after the default changes.
    limits: tuple[tuple[str, int], ...] = ()

    def quota_limits(self) -> Limits:
        return Limits.merge(dict(self.limits))

    def owner(self, home: Path | None = None) -> Owner:
        """This account's tenancy, optionally rooted somewhere other than `~`.

        `home` exists because a server is not obliged to keep its tenants in
        the operator's home directory -- `minicodex serve --home /srv/tenants`
        is a reasonable thing to want, and a test that must not write into the
        real `~/.minicodex` needs exactly the same hook. `None` keeps chapter
        20's default, which is the path the CLI has always used.
        """
        return Owner(self.key) if home is None else Owner(self.key, home=home)

    def public(self) -> dict[str, Any]:
        """What the browser is allowed to know. No hash, ever."""
        return {
            "key": self.key,
            "admin": self.admin,
            "disabled": self.disabled,
            "created": self.created,
            "workspace_roots": list(self.workspace_roots),
            "limits": dict(self.limits),
        }


def _from_json(raw: dict[str, Any]) -> Account:
    return Account(
        key=raw["key"],
        password_hash=raw["password_hash"],
        created=float(raw.get("created", 0.0)),
        admin=bool(raw.get("admin", False)),
        disabled=bool(raw.get("disabled", False)),
        workspace_roots=tuple(raw.get("workspace_roots") or ()),
        limits=tuple(sorted((raw.get("limits") or {}).items())),
    )


def _to_json(account: Account) -> dict[str, Any]:
    return {
        "key": account.key,
        "password_hash": account.password_hash,
        "created": account.created,
        "admin": account.admin,
        "disabled": account.disabled,
        "workspace_roots": list(account.workspace_roots),
        "limits": dict(account.limits),
    }
```

> - **`RESERVED_KEYS`**：账号名会变成目录名，和控制台自己的文件放在一起，这几个名字不能用。
> - **`Account`**：`frozen=True`。`admin`（只能管账号，**不能**读别人的东西——注释说"一个控制台做不到的越权，比一个只给对的人做的越权好"）、`disabled`、`workspace_roots`（§13）、`limits`（§12）。
> - **`owner(home)`**：这个账号的 `Owner`——第 20 章那个类型第一次被一个**认证过的**东西构造出来。
> - **`public()`**：给浏览器看的字段，**没有哈希**。

### 3.2 改写时发现：一个"偷到 `accounts.json` 的人"不需要偷

`AccountStore` 读写那个文件：

```python
@dataclass
class AccountStore:
    """`accounts.json`, and the bootstrap token that exists while it is empty.

    Not a `Store` table. `Store` is the console's *per-owner* record store
    after this chapter, and the list of owners cannot live inside one of them;
    the two also want different durability, because a half-written
    `threads.json` costs a session and a half-written `accounts.json` costs
    everybody the ability to log in.
    """

    path: Path
    #: Printed once at startup while there is nobody to log in as. Held in
    #: memory only: a bootstrap token written to disk is a password file with
    #: the door left open, and a restart is the right way to get a new one.
    bootstrap_token: str | None = field(default=None, repr=False)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Deliberately *not* the `_load` degradation `store.py` uses. There
            # a corrupt file means an empty list of threads; here it would mean
            # an empty list of accounts, which this class treats as "nobody has
            # registered yet" -- and that would turn a truncated write into a
            # console that hands out a fresh bootstrap token to whoever asks.
            raise AccountError(
                f"{self.path} is unreadable; refusing to treat it as empty"
            ) from None
        return list(data) if isinstance(data, list) else []

    def _write(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        # 0600, for the reason this module's docstring gives for scrypt: "an
        # attacker with a stolen accounts.json pays per guess".  The first
        # version made the file readable by every user on the machine, which
        # is most of the way to stolen.  Found when chapter 23 was rewritten.
        with open_private(tmp) as handle:
            handle.write(json.dumps(records, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)

    # -- reading -------------------------------------------------------------

    def all(self) -> list[Account]:
        return [_from_json(raw) for raw in self._read()]

    def get(self, key: str) -> Account | None:
        return next((a for a in self.all() if a.key == key), None)

    def empty(self) -> bool:
        return not self._read()
```

> - **`_read`**：注释讲了为什么一个读不了的 `accounts.json` **不能**被当成空的——空表示"还没人注册"，那会让控制台重新发一个 bootstrap 令牌给任何来要的人。
> - **`_write`**：先写到 `.tmp`、`fsync`、再改名盖过去（第 21 章 §8.2 讲过为什么）。

`accounts.py` 的说明为 scrypt 的成本辩护时说："一个偷到 `accounts.json` 的人，每猜一次都要付这笔钱。"第一版的 `_write` 是这样打开那个临时文件的：

```python
        with open(tmp, "w", encoding="utf-8") as handle:
```

按进程的 umask 创建——大多数机器上是 0644：

```
$ uv run python -c "...AccountStore(p).create('alice', 'alice-password-1'); print(oct(p.stat().st_mode & 0o777))"
0o644
```

**这台机器上的每一个用户都能读它。**"偷"这一步不存在。同一件事也发生在每个账号的 `tenants/<key>/providers.json` 上——那里是**明文的** API key（第 22 章 §7 说过"控制台的存储是明文的"，但没说是谁都能读的明文）。

修法和第 21 章的 token 文件一样：创建时就是 0600。一个小函数放在 `store.py` 里，两边都用：

```python
def open_private(path: Path) -> Any:
    """Open `path` for writing, created 0600 -- and 0600 again if it already
    existed with a wider mode (a temporary file a crash left behind)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")
```

```python
@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_consoles_files_are_not_readable_by_other_users(
    console: TestClient, tmp_path: Path
) -> None:
    """`accounts.json` holds password hashes and `providers.json` API keys;
    the first version created both with the umask's mode (0644)."""
    old = os.umask(0o022)
    try:
        console.post(
            "/api/providers",
            json={"name": "p", "base_url": "https://x.test/v1", "model": "m", "api_key": "sk-x"},
        )
        _second_account(console, "carol")
    finally:
        os.umask(old)
    data = tmp_path / "console"
    for path in (data / "accounts.json", data / "tenants" / CONSOLE_ACCOUNT / "providers.json"):
        assert path.stat().st_mode & 0o077 == 0, path
```

> umask 设成 022（最常见的值），存一个模型来源、建一个账号，两个文件的"组/其他人"权限位都是 0。

`AccountStore` 的其余部分：

```python
def create(
    self,
    key: str,
    password: str,
    *,
    admin: bool = False,
    workspace_roots: tuple[str, ...] = (),
) -> Account:
    key = key.strip().lower()
    if not _HANDLE.match(key):
        raise AccountError(
            f"{key!r} is not a usable account name: 1-64 characters of "
            "a-z, 0-9, hyphen or underscore, starting with a letter or digit"
        )
    if key in RESERVED_KEYS:
        raise AccountError(f"{key!r} is reserved")
    try:
        Owner(key)
    except OwnerError as exc:  # pragma: no cover - _HANDLE is the stricter of the two
        raise AccountError(str(exc)) from exc
    records = self._read()
    if any(r["key"] == key for r in records):
        raise AccountError(f"account {key!r} already exists")
    account = Account(
        key=key,
        password_hash=hash_password(password),
        created=time.time(),
        admin=admin,
        workspace_roots=workspace_roots,
    )
    records.append(_to_json(account))
    self._write(records)
    return account

def update(self, key: str, **changes: Any) -> Account | None:
    records = self._read()
    found = None
    for raw in records:
        if raw["key"] == key:
            raw.update(changes)
            found = _from_json(raw)
    if found is not None:
        self._write(records)
    return found

def set_password(self, key: str, password: str) -> Account | None:
    return self.update(key, password_hash=hash_password(password))
```

> - **`create`**：名字统一转小写、检查格式和保留字、再让 `Owner` 自己检查一遍（第 20 章 §9.3 的规则）、不能重名、存哈希。
> - **`update`**：改一个账号的几个字段（停用、换口令都走它）。

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


```python
def issue_bootstrap_token(self) -> str | None:
    """A one-time token, in memory, only while there is nobody to log in as.

    The alternatives are worse in a way that is worth spelling out. A
    default password (`admin`/`admin`) is a published credential on every
    install that skipped the second step. An unauthenticated "create the
    first account" route is the same thing without the password: whoever
    reaches the port first becomes the operator, and on a machine with any
    other user on it that is not necessarily you. A token printed to the
    terminal that started the process binds the first account to whoever
    can read that terminal, which is the same person who decided to start
    the server. Jupyter reached this conclusion first; the reasoning is
    general.
    """
    if not self.empty():
        self.bootstrap_token = None
        return None
    if self.bootstrap_token is None:
        self.bootstrap_token = secrets.token_urlsafe(24)
    return self.bootstrap_token


def redeem_bootstrap(self, token: str, key: str, password: str) -> Account:
    expected = self.bootstrap_token
    if expected is None or not self.empty():
        raise AccountError("this console already has an account; sign in instead")
    if not hmac.compare_digest(token, expected):
        raise AccountError("that is not the bootstrap token this server printed")
    account = self.create(key, password, admin=True)
    # Spent. A token that still works after it has been used is a second
    # password with none of a password's protections.
    self.bootstrap_token = None
    return account
```

> - **`issue_bootstrap_token`**：只在没有任何账号时发；已经发过就返回同一个。
> - **`redeem_bootstrap`**：令牌对（`compare_digest`）而且还没有账号——建第一个账号，**管理员**；然后把令牌清掉。

测试：

```python
def test_the_bootstrap_token_is_one_time_and_not_on_disk(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "accounts.json")
    token = store.issue_bootstrap_token()
    assert token and len(token) > 20
    assert not (tmp_path / "accounts.json").exists()
    store.redeem_bootstrap(token, "alice", "a-long-enough-password")
    with pytest.raises(AccountError):
        store.redeem_bootstrap(token, "eve", "a-long-enough-password")
    # And it is gone from the process, not merely no longer accepted. The
    # second `redeem_bootstrap` above is refused by `self.empty()` either way,
    # so without this line nothing tested the clearing -- the mutation probe
    # found that. A spent credential left in an attribute is one `repr()` or
    # one debug route away from being readable, which is the same argument
    # `sessions.py` makes for storing token hashes in an in-memory table.
    assert store.bootstrap_token is None


def test_a_wrong_bootstrap_token_creates_nothing(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "accounts.json")
    store.issue_bootstrap_token()
    with pytest.raises(AccountError):
        store.redeem_bootstrap("not-the-token", "eve", "a-long-enough-password")
    assert store.empty()


def test_the_first_account_is_an_admin_and_the_next_one_is_not(console: TestClient) -> None:
    assert console.get("/api/auth/status").json()["account"]["admin"] is True
    _second_account(console)
    accounts = {a["key"]: a for a in console.get("/api/accounts").json()}
    assert accounts["bob"]["admin"] is False
    assert "password_hash" not in accounts["bob"]


def test_an_unreadable_accounts_file_is_not_an_empty_one(tmp_path: Path) -> None:
    """`store.py` degrades a corrupt file to its default; this must not.

    An empty list of threads is a lost session. An empty list of *accounts* is
    a console that decides nobody has registered yet and hands a fresh
    bootstrap token to whoever asks for one.
    """
    path = tmp_path / "accounts.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = AccountStore(path)
    with pytest.raises(AccountError):
        store.all()
```

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
#: Reachable without a session. Every one of these is either about *becoming*
#: authenticated or about telling a browser which of those two it should show.
#:
#: `/api/auth/status` is public and returns almost nothing on purpose: whether
#: this console has any account yet (so the frontend knows to render the
#: bootstrap form instead of the login form) and whether the caller is signed
#: in. It does not say which accounts exist.
PUBLIC_PATHS = frozenset(
    {
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/bootstrap",
    }
)

#: Guarded prefixes. Everything else -- `/`, `/assets/...`, the built frontend
#: -- is served without a session, because the login page has to come from
#: somewhere and a bundle of JavaScript is not a secret. What it can *do*
#: without a session is nothing: every call it makes lands under `/api`.
GUARDED_PREFIXES = ("/api", "/ws")

#: Methods that do not change anything, so a cross-origin one needs no check.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
```

> - **`PUBLIC_PATHS`**：不需要登录的三个路径——查状态、登录、用 bootstrap 令牌建第一个账号。
> - **`GUARDED_PREFIXES`**：要守的前缀。`/`、`/assets/...`（前端本身）不守：登录页得从某个地方来，一包 JavaScript 不是秘密。
> - **`_SAFE_METHODS`**：§7.1 加的。

现在"忘了"是**向关的方向失败**的：一个本该公开、但没进名单的新路由，第一个
请求就返回 401——一个会自己报告自己的 bug。

而名单也不能悄悄变长。测试走 `app.routes` 自己那张表：

那张"走一遍所有路由"的表，和测试本身：

```python
def _every_route(app: Any) -> list[Any]:
    """Flatten the app's route table, including the included router.

    This version of FastAPI wraps `include_router` in a `_IncludedRouter` that
    holds its own `.routes`, so walking `app.routes` one level deep finds six
    entries -- `/openapi.json`, the docs, and the static fallback -- and none of
    the thirty-odd routes the console actually serves. The first version of the
    walk below did exactly that and passed, which is the failure mode chapter
    19 (F19-06) already had once: a check that silently stops covering the code
    it names. The `checked > 20` assertion at the bottom of the test is there
    because of it.
    """
    found: list[Any] = []
    stack = list(getattr(app, "routes", []))
    while stack:
        route = stack.pop()
        # `routes` for a `Mount`, `original_router` for the `_IncludedRouter`
        # this FastAPI version puts in the table for `include_router`. Both
        # spellings, because which one exists is a fact about the installed
        # version and this test is about the console.
        for attr in ("routes", "original_router"):
            child = getattr(route, attr, None)
            if child is not None:
                stack.extend(child if isinstance(child, list) else getattr(child, "routes", []))
        if hasattr(route, "path"):
            found.append(route)
    return found


def _thread(client: TestClient, workspace: Path, **settings: Any) -> str:
    response = client.post(
        "/api/threads",
        json={"workspace": str(workspace), "title": "test", "settings": settings},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_F23_01_every_guarded_route_refuses_an_anonymous_caller(anon: TestClient) -> None:
    """Walk the app's own route table rather than a list written by hand.

    This is the mechanism, not a spot check. A route added next year is in
    `app.routes` the moment it is written, so it is in this test the moment it
    is written, and the only way to make it public is to put its path in
    `PUBLIC_PATHS` -- where a reviewer looking for the console's anonymous
    surface will find it, because that is the whole list.
    """
    checked = 0
    for route in _every_route(anon.app):
        path = getattr(route, "path", "")
        if not path.startswith(GUARDED_PREFIXES) or path in PUBLIC_PATHS:
            continue
        if "{" in path:
            # Path parameters get a value that cannot exist, so a 401 is the
            # gate answering rather than the route 404ing on the way past it.
            path = path.replace("{thread_id}", "nope").replace("{server_id}", "nope")
            path = path.replace("{provider_id}", "nope").replace("{approval_id}", "nope")
            path = path.replace("{session_id}", "nope").replace("{key}", "nope")
            path = path.replace("{index}", "0")
        methods = getattr(route, "methods", None) or {"GET"}
        method = sorted(methods - {"HEAD", "OPTIONS"})[0]
        response = anon.request(method, path, json={})
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"
        checked += 1
    assert checked > 20, f"only {checked} guarded routes were found; the walk is not working"


def test_F23_01_the_public_list_is_short_and_is_all_about_signing_in(anon: TestClient) -> None:
    assert PUBLIC_PATHS == {
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/bootstrap",
    }
    for path in PUBLIC_PATHS:
        method = "GET" if path.endswith("status") else "POST"
        assert anon.request(method, path, json={}).status_code != 401


def test_F23_01_the_frontend_bundle_is_not_behind_the_gate(anon: TestClient) -> None:
    """The login page has to come from somewhere.

    Everything outside `/api` and `/ws` is served without a session, and what
    it can do without one is nothing: every call it makes is guarded.
    """
    assert anon.get("/").status_code == 200


def test_F23_01_status_says_whether_a_console_is_claimed_and_nothing_else(
    anon: TestClient,
) -> None:
    fresh = anon.get("/api/auth/status").json()
    assert fresh == {"has_account": False, "signed_in": False, "account": None}
    sign_in(anon)
    claimed = anon.get("/api/auth/status").json()
    assert claimed["signed_in"] is True
    assert claimed["account"]["key"] == CONSOLE_ACCOUNT
    assert "password_hash" not in claimed["account"]
```

> - **`_every_route`**：把 FastAPI 的路由表**展开**——`include_router` 包了一层，要钻进 `routes` 和 `original_router` 里去（下面那一节讲为什么）。
> - **第一个测试**：每个守着的路由，用一个没登录的客户端去请求，必须是 401；带路径参数的随便填一个值；最后 `checked > 20`。
> - **第二个**：公开名单就是那三个，每个都和登录有关。
> - **第四个**：`/api/auth/status` 只说"有没有账号""你登没登录"，不列账号。

这次改写重数了一遍：

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


## 6. F23-02：一个真的测量——中间件盖不住 WebSocket

给 Starlette 应用加中间件，教程写法是 `BaseHTTPMiddleware`。

它 dispatch 的是 `scope["type"] == "http"`，其他类型**原样放过去**。

也就是说：一个这样守着的控制台，会认证每一个 REST 调用，然后把
`/ws/threads/{id}` 敞着——那条 socket 上流的是模型的输出、它跑的每一条 shell
命令、以及每一条命令的结果。

这句话是断言还是事实，取决于有没有量过。`probe_ws_auth.py` 就是去量它的：
同样两个路由，两种关卡，不带任何凭证去敲。


```
$ uv run python probe_ws_auth.py
gate                   GET /api/secret    ws /ws/secret
------------------------------------------------------------------------------
BaseHTTPMiddleware     HTTP 401           OPENED, received 'the socket route ran'
raw ASGI               HTTP 401           refused (WebSocketDisconnect)

Both gates deny every HTTP request. Only one of them is a gate.
```

（这次改写在 Linux 上重跑的，和第一版的结果一样。）

所以这个关卡是**裸 ASGI** 写的，`http` 和 `websocket` 走同一个判断。

裸 ASGI 的代价也在那个探针里，如实写着：第一版忘了把 `lifespan` 放过去，
应用根本起不来。自己处理每一种 scope 类型，包括那个不是请求的。

**拒绝要发生在 `accept()` 之前。** `websocket.close` 在 accept 之前，对握手
来说是一个 HTTP 403，浏览器的 `WebSocket` 构造函数报的是"连接失败"；先 accept
再 close，则是告诉浏览器"你连上了"——而照着那个行为写的客户端，会去重试一条
它从来就不该拥有的 socket。


`probe_ws_auth.py` 全文：

```python
#!/usr/bin/env python
"""Does a `BaseHTTPMiddleware` gate cover the WebSocket? Measured, not assumed.

    uv run python probe_ws_auth.py

`auth.py` says the console's gate is raw ASGI "because of the socket", and that
claim is worth exactly as much as the measurement behind it. So this builds two
apps with the same routes -- one HTTP route and one WebSocket route -- and puts
a different gate in front of each:

* `BaseHTTPMiddleware`, the way most FastAPI tutorials add authentication;
* the console's own `AuthMiddleware`, raw ASGI.

Then it knocks on both doors of both apps with no credential at all and prints
what happened. Offline: `TestClient` speaks ASGI in-process, no port is bound.

The interesting cell is the top right. If it says the socket opened, then a
console guarded that way authenticates every REST call and streams the model's
output, the commands it runs and their results to anybody who can name a
thread.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware


def build(kind: str) -> FastAPI:
    app = FastAPI()

    @app.get("/api/secret")
    def secret() -> dict[str, str]:
        return {"secret": "the REST route ran"}

    @app.websocket("/ws/secret")
    async def socket(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text("the socket route ran")
        await ws.close()

    if kind == "base":

        async def deny(request: Any, call_next: Any) -> Any:
            return JSONResponse({"detail": "not signed in"}, status_code=401)

        app.add_middleware(BaseHTTPMiddleware, dispatch=deny)
    else:

        class Deny:
            def __init__(self, app: Any) -> None:
                self.app = app

            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] not in ("http", "websocket"):
                    # `lifespan` goes past. The first version of this probe
                    # left it out and the app would not start -- which is the
                    # cost of raw ASGI stated honestly: you handle every scope
                    # type yourself, including the one that is not a request.
                    await self.app(scope, receive, send)
                    return
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008, "reason": "no"})
                    return
                body = b'{"detail":"not signed in"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})

        app.add_middleware(Deny)
    return app


def knock(app: FastAPI) -> tuple[str, str]:
    with TestClient(app) as client:
        http = f"HTTP {client.get('/api/secret').status_code}"
        try:
            with client.websocket_connect("/ws/secret") as ws:
                got = ws.receive_text()
            socket = f"OPENED, received {got!r}"
        except Exception as exc:
            socket = f"refused ({type(exc).__name__})"
    return http, socket


def main() -> int:
    print(f"{'gate':<22} {'GET /api/secret':<18} ws /ws/secret")
    print("-" * 78)
    for kind, label in (("base", "BaseHTTPMiddleware"), ("asgi", "raw ASGI")):
        http, socket = knock(build(kind))
        print(f"{label:<22} {http:<18} {socket}")
    print()
    print("Both gates deny every HTTP request. Only one of them is a gate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> - 一个最小的应用：一个 HTTP 路由、一个 WebSocket 路由。两种关卡各包一次：`BaseHTTPMiddleware`（教程写法）和裸 ASGI（这一章的写法）。
> - 各用一个没有凭证的客户端去敲两个路由，打印结果。

这一章的关卡——`auth.py` 的核心：

```python
@dataclass(frozen=True)
class AuthContext:
    """Who this request is, resolved once, at the door."""

    account: Account
    session: Session
    #: The raw cookie, kept so `logout` can revoke exactly this session
    #: without the route having to read the header again.
    token: str
    #: Where this server keeps its tenants' home directories.
    home: Path = MINICODEX_HOME

    @property
    def owner(self) -> Owner:
        """The tenancy. The whole reason chapter 20 wrote this type."""
        return self.account.owner(self.home)


def resolve(
    scope_headers: list[tuple[bytes, bytes]],
    accounts: AccountStore,
    table: SessionTable,
    home: Path = MINICODEX_HOME,
) -> AuthContext | None:
    """Cookie -> session -> account, or `None` at the first step that fails.

    Returns `None` for a missing cookie, an unknown or expired session, a
    session whose account has been deleted, and a session whose account has
    been disabled -- one answer for all five, and the last two matter: a
    session table that outlives the account list would keep somebody signed in
    after they were removed, which is how "we revoked their access" turns into
    a still-working browser tab.
    """
    token = _cookie(scope_headers, SESSION_COOKIE)
    if not token:
        return None
    session = table.touch(token)
    if session is None:
        return None
    account = accounts.get(session.account_key)
    if account is None or account.disabled:
        table.revoke(token)
        return None
    return AuthContext(account=account, session=session, token=token, home=home)


def _cookie(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    for key, value in headers:
        if key.lower() != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            head, _, tail = part.strip().partition("=")
            if head == name:
                return tail
    return None


def _header(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


class AuthMiddleware:
    """Raw ASGI, so `http` and `websocket` go through the same decision."""

    def __init__(self, app: Any, *, console_of: Any) -> None:
        self.app = app
        # A callable rather than the console itself: the middleware is built
        # while the app is being assembled, and `app.state.console` is set in
        # the same function. Reaching for it per request also means a test that
        # swaps the console on a live app is not lying to the gate.
        self._console_of = console_of

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith(GUARDED_PREFIXES) or path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        console = self._console_of()
        headers = scope.get("headers") or []

        if scope["type"] == "websocket" and not same_origin(headers):
            await _reject_socket(send, "cross-origin websocket handshake refused")
            return
        # The same check for a request that changes something.  `SameSite=Lax`
        # keeps the cookie off a *cross-site* POST, but "site" ignores the
        # port: a page on `http://localhost:3000` is same-site with this
        # console on `localhost:8000`, its POST carries the cookie, and a POST
        # with no body is a "simple" request that needs no CORS preflight.
        # The first version checked `Origin` only on the socket, so such a page
        # could disable accounts, sign everybody out or cancel turns.  Found
        # when chapter 23 was rewritten.  (A JSON body is safe already: FastAPI
        # answers 422 to `text/plain` or a form, the only bodies a page can
        # send without a preflight.)
        if scope["type"] == "http" and scope.get("method") not in _SAFE_METHODS:
            if not same_origin(headers):
                await _reject_http(send, status=403, detail="cross-origin request refused")
                return

        auth = resolve(headers, console.accounts, console.sessions, console.home)
        if auth is None:
            if scope["type"] == "websocket":
                await _reject_socket(send, "not signed in")
            else:
                await _reject_http(send)
            return

        # `scope["state"]` is what Starlette turns into `request.state`, and
        # writing it here is what makes `require(request)` below a lookup
        # rather than a second resolution. One authentication per request.
        scope.setdefault("state", {})["auth"] = auth
        await self.app(scope, receive, send)


async def _reject_http(send: Any, *, status: int = 401, detail: str = "not signed in") -> None:
    body = json.dumps({"detail": detail}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _reject_socket(send: Any, reason: str) -> None:
    """Close without accepting.

    `websocket.close` before `websocket.accept` is an HTTP 403 on the
    handshake, which is what a browser's `WebSocket` constructor reports as an
    error rather than as a socket that opens and immediately closes. Accepting
    first and closing after would have let the client believe, for one round
    trip, that it was connected.
    """
    await send({"type": "websocket.close", "code": 1008, "reason": reason})


def require(request: Request) -> AuthContext:
    """The authenticated caller, or a 500 that means the gate did not run.

    Not a 401. If this raises, the request reached a guarded route without
    passing the middleware, which is a wiring bug in this program and not
    something the caller did wrong -- answering 401 would send a person off to
    log in again over a mistake no login can fix.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None:  # pragma: no cover - the middleware makes this unreachable
        raise HTTPException(500, "this route is not behind the authentication gate")
    return auth
```

> - **`AuthContext`**：一个请求是谁——账号、会话、原始 cookie（登出时要用）、`home`。`owner` 属性就是这个账号的 `Owner`。
> - **`resolve`**：cookie → 会话 → 账号；任何一步失败都返回 `None`。账号被删了或被停用了，顺手把会话也撤掉。
> - **`AuthMiddleware.__call__`**：
>   - 不是 `http`/`websocket`（比如 `lifespan`）就直接放过——第一版的探针就是在这里摔的（上面那段）。
>   - 不在守着的前缀下、或者在公开名单里：放过。
>   - WebSocket 不同源：拒绝（§7）。不安全的 HTTP 方法不同源：拒绝（§7.1，这次改写加的）。
>   - `resolve` 失败：WebSocket 关闭、HTTP 401。
>   - 成功：把 `AuthContext` 放进 `scope["state"]`，路由里 `require(request)` 直接取。
> - **`_reject_http`**：手写一个 JSON 响应（状态码和内容可以指定——§7.1 加的参数）。**`_reject_socket`**：在 `accept` 之前 `close`，浏览器看到的是握手失败。
> - **`require`**：路由取"是谁"的唯一方法。取不到是 500 而不是 401——那说明这个路由没在关卡后面，是程序的接线错误，不是用户该去重新登录。

测试：

```python
def test_F23_02_an_unauthenticated_socket_is_refused(anon: TestClient, tmp_path: Path) -> None:
    with pytest.raises(Exception):  # noqa: B017 - any refusal; the point is it does not open
        with anon.websocket_connect("/ws/threads/whatever"):
            pass


def test_F23_02_a_signed_in_socket_opens_on_its_own_thread(
    console: TestClient, workspace: Path
) -> None:
    thread_id = _thread(console, workspace)
    with console.websocket_connect(f"/ws/threads/{thread_id}") as ws:
        assert ws is not None


def test_F23_02_the_probe_that_measured_this_is_in_the_step(repo_root: Path) -> None:
    """`auth.py` says raw ASGI "because of the socket" and cites a probe.

    A docstring that cites a measurement has to be able to point at it, or it
    is an assertion wearing a measurement's clothes. Chapters 6, 14, 16, 17, 18
    and 20 each found their own measuring tool wrong; the cheapest defence
    against the next one is that the tool exists and is runnable.
    """
    probe = repo_root / "probe_ws_auth.py"
    assert probe.exists()
    assert "BaseHTTPMiddleware" in probe.read_text(encoding="utf-8")
```

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

```python
def same_origin(headers: list[tuple[bytes, bytes]]) -> bool:
    """Is this handshake from a page the console itself served?

    No `Origin` at all is allowed through: that is a non-browser client (a
    script, a test, `websocat`), which is not the attack this check exists
    for. The attack needs a *browser* to attach the cookie, and a browser
    always sends `Origin` on a WebSocket handshake.
    """
    origin = _header(headers, "origin")
    if origin is None:
        return True
    host = _header(headers, "host")
    if not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()
```

```python
def test_F23_03_a_cross_origin_handshake_is_refused(console: TestClient, workspace: Path) -> None:
    """The cookie would be attached by the browser; the origin is what stops it.

    `SameSite=Lax` covers cross-site form posts, and this is the hole it does
    not cover: a page on any origin may open a WebSocket to this console, and
    the browser sends the cookie with the handshake. So the handshake is where
    the origin is compared to the host.
    """
    thread_id = _thread(console, workspace)
    with pytest.raises(Exception):  # noqa: B017
        with console.websocket_connect(
            f"/ws/threads/{thread_id}", headers={"Origin": "https://evil.example"}
        ):
            pass


def test_F23_03_the_consoles_own_page_is_same_origin(console: TestClient, workspace: Path) -> None:
    thread_id = _thread(console, workspace)
    with console.websocket_connect(
        f"/ws/threads/{thread_id}", headers={"Origin": "http://testserver"}
    ) as ws:
        assert ws is not None


def test_F23_03_a_handshake_with_no_origin_at_all_is_allowed() -> None:
    """A non-browser client is not the attack this check exists for.

    `websocat`, a script, this test suite: none of them is carrying somebody
    else's cookies, because nothing is filling a cookie jar for them. The
    attack needs a browser, and a browser always sends `Origin`.
    """
    assert same_origin([(b"host", b"console.local")]) is True
    assert same_origin([(b"host", b"console.local"), (b"origin", b"http://console.local")]) is True
    assert same_origin([(b"host", b"console.local"), (b"origin", b"http://evil.test")]) is False


def test_F23_03_an_origin_with_no_host_header_is_refused() -> None:
    """Nothing to compare against is not a reason to allow it."""
    assert same_origin([(b"origin", b"http://console.local")]) is False
```

### 7.1 改写时发现：同站不等于同源

`auth.py` 的说明（第一版）这样分工："cookie 加上 `SameSite=Lax` 就能挡住跨站的表单提交"，所以 `Origin` 检查只给 WebSocket。`set_session_cookie` 的说明更直接："`Lax` 就是这个控制台的 CSRF 防御。"

`Lax` 挡的是**跨站**。"站"只看协议和注册域名，**不看端口**——`http://localhost:3000` 上的任何一个页面（另一个开发服务器、一个 `python -m http.server` 打开的 HTML 文件）和 `http://localhost:8000` 上的控制台是**同站**的。它发给控制台的 POST，浏览器会带上 cookie。

剩下的那道门是 CORS 预检：一个"非简单"请求（比如 `Content-Type: application/json`）会先发一个 `OPTIONS` 去问，控制台不回答允许，浏览器就不发。先量一下，那道门在哪儿：

```
text/plain                               422 {"detail":[{"type":"model_attributes_type","loc":["body"],"msg":"Input should be
application/x-www-form-urlencoded        422 {"detail":[{"type":"model_attributes_type","loc":["body"],"msg":"Input should be
multipart/form-data; boundary=a          422 {"detail":[{"type":"model_attributes_type","loc":["body"],"msg":"Input should be
```

（一个登录过的客户端，用三种"简单请求"能用的 `Content-Type` 把一段 JSON 发给 `POST /api/mcp`。）

FastAPI 拒绝把它们当 JSON 解析——**带正文的路由是安全的**，这是一个负结果。可这个控制台有一批**没有正文**的 POST：

- `POST /api/accounts/{key}/disable`——停用一个账号（管理员登录着的时候）；
- `POST /api/auth/sessions/revoke-all`——把这个人在所有设备上签出、取消他所有正在跑的回合；
- `POST /api/threads/{id}/cancel`、`POST /api/providers/{id}/activate`……

没有正文的 POST 是一个"简单请求"，不需要预检，浏览器直接发、带着 cookie。响应它读不到，但事情已经做了。

修法：同一个 `same_origin` 检查，用在**会改东西的** HTTP 方法上（上面 `AuthMiddleware` 里那一段）。`GET` 不受影响。控制台自己的页面发的 POST，`Origin` 就是它自己的主机，照常通过。

一个配套改动：前端开发模式下，Vite 的代理原来对 `/api` 设了 `changeOrigin: true`——它把 `Host` 改成后端的地址，`Origin` 不动，两者就对不上了：

```typescript
proxy: {
  // No `changeOrigin`: it rewrites `Host` to the target and leaves
  // `Origin` alone, and the server refuses a state-changing request whose
  // `Origin` does not match its `Host` (chapter 23, rewritten).
  "/api": { target: "http://127.0.0.1:8000" },
  "/ws": { target: "ws://127.0.0.1:8000", ws: true },
```

测试——正例、反例，和那个负结果：

```python
def test_a_same_site_page_cannot_post_to_the_console(console: TestClient) -> None:
    """`SameSite=Lax` ignores the port, so a page on another `localhost` port
    sends the cookie with its POST -- and a POST with no body needs no CORS
    preflight.  The first version checked `Origin` only on the socket."""
    host = "testserver"
    refused = console.post(
        "/api/auth/sessions/revoke-all", headers={"origin": "http://localhost:3000", "host": host}
    )
    assert refused.status_code == 403
    assert console.get("/api/auth/sessions").status_code == 200, "nothing was revoked"
    # Reading is not changed by the check; the console's own page still writes.
    assert (
        console.get("/api/auth/sessions", headers={"origin": "http://localhost:3000"}).status_code
        == 200
    )
    ok = console.post(
        "/api/auth/sessions/revoke-all", headers={"origin": f"http://{host}", "host": host}
    )
    assert ok.status_code == 200


def test_a_json_body_was_never_the_way_in() -> None:
    """The negative half, measured rather than assumed: the only bodies a page
    can send without a preflight are `text/plain` and forms, and FastAPI
    refuses both for a route that takes a model.  Kept as a test so that a
    route which starts reading `request.body()` itself does not quietly
    remove the reason the check above is enough."""
    import tempfile

    from console_login import sign_in as _sign_in

    tmp = Path(tempfile.mkdtemp())
    with TestClient(create_app(tmp / "c", home=tmp / "h")) as client:
        _sign_in(client)
        body = json.dumps({"name": "x", "command": "true"})
        for content_type in ("text/plain", "application/x-www-form-urlencoded"):
            response = client.post("/api/mcp", content=body, headers={"content-type": content_type})
            assert response.status_code == 422, content_type
```

> - 第一个：带着 `Origin: http://localhost:3000` 的 `revoke-all` 是 403，会话还在；同一个页面的 `GET` 照常；带着控制台自己 `Origin` 的 `revoke-all` 是 200。
> - 第二个：把"带正文的路由不怕"这件事钉成测试——哪天有个路由自己去读 `request.body()`，这个前提就不再成立，这个测试会提醒。

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


`app.py` 里的 `Console`，拆开以后：

```python
@dataclass
class Console:
    """Everything one `minicodex serve` process owns."""

    #: The root of the console's own state. Accounts live here; every record
    #: store lives one level down under `tenants/<key>/`.
    data_dir: Path
    accounts: AccountStore
    #: Where tenants' home directories live: `<home>/tenants/<key>/` holds the
    #: memories, the memory-job table and the MCP tokens that chapter 20's
    #: `Owner` names. Defaults to `~/.minicodex`, so a single-account console
    #: on a laptop shares one home with the CLI, byte for byte.
    home: Path = MINICODEX_HOME
    sessions: SessionTable = field(default_factory=SessionTable)
    quota: QuotaLedger = field(default_factory=QuotaLedger)
    channel: Channel = field(default_factory=Channel)
    #: Turns with a run in flight, keyed by `turn_key`. Claimed synchronously
    #: in the route, which is the whole point -- see routes.py.
    busy: set[str] = field(default_factory=set)
    #: The task per busy turn, so the stop button has something to cancel and
    #: so nothing collects a running task.
    running: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    #: Chapter 22's OAuth attempts that have a `start` but no `callback` yet.
    #: One table, because an attempt already carries the owner it belongs to
    #: and `PendingOAuthTable.pop` is already the only lookup that is trusted.
    mcp_oauth: PendingOAuthTable = field(default_factory=PendingOAuthTable)

    _stores: dict[str, Store] = field(default_factory=dict, repr=False)
    _brokers: dict[str, ApprovalBroker] = field(default_factory=dict, repr=False)

    # -- per-owner state ------------------------------------------------------

    def store_for(self, owner: Owner) -> Store:
        """This owner's records, seeded on first touch.

        `~/.minicodex/tenants/<key>/` (chapter 20's `Owner.root`) and
        `<data-dir>/tenants/<key>/` (here) are two different trees keyed by the
        same string, and they stay separate for the same reason they were
        separate when there was one user: one is the agent's home directory,
        which the CLI shares, and the other is this server's own bookkeeping,
        which the CLI has never heard of.
        """
        key = owner.key or ""
        store = self._stores.get(key)
        if store is None:
            root = self.data_dir if owner.key is None else self.data_dir / TENANTS_DIR / owner.key
            store = Store(root)
            store.seed_providers()
            self._stores[key] = store
        return store

    def broker_for(self, account_key: str) -> ApprovalBroker:
        broker = self._brokers.get(account_key)
        if broker is None:
            broker = ApprovalBroker()
            self._brokers[account_key] = broker
        return broker

    def running_for(self, account_key: str) -> int:
        prefix = f"{account_key}/"
        return sum(1 for key in self.busy if key.startswith(prefix))

    # -- lookups --------------------------------------------------------------

    def require_thread(self, owner: Owner, thread_id: str) -> dict[str, Any]:
        """A thread of *this* owner's, or 404.

        There is no ownership check in this method, and that is not an
        omission: `store_for` returned a store rooted at one directory, so a
        thread belonging to somebody else is not absent-and-forbidden, it is
        absent. The 404 is the truth rather than a cover story, which is also
        why it does not leak whether the id exists elsewhere.
        """
        record = self.store_for(owner).thread(thread_id)
        if record is None:
            raise HTTPException(404, f"no thread {thread_id!r}")
        return record
```

> - **`store_for(owner)`**：这个人的记录，`<data-dir>/tenants/<key>/`。第一次用到时建出来。
> - **`broker_for(account_key)`**：这个人的审批代理（第 19 章）。
> - **`running_for(account_key)`**：这个人有几个回合在跑——数 `busy` 里以 `<key>/` 开头的（§12 用）。
> - **`require_thread`**：在**这个人的** store 里找 thread，找不到就 404。

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


会话表全文（`sessions.py`）。说明讲了三件事：会话不落盘（§15）、表里存的是哈希、为什么是 SHA-256 而不是 scrypt：

```python
"""Signed-in browsers: one table, in this process, with two clocks.

Chapter 22 needed somewhere to keep an OAuth attempt between two HTTP requests
and built `PendingOAuthTable` -- an in-process dict with a TTL and a lazy
sweep. This is the same shape holding a longer-lived thing, and the shape is
not a coincidence: the console's whole model is that one `serve` process owns
everything it is doing, which is already true of the approval broker, the
running turns and the event channel.

**Sessions are not persisted, deliberately.** A restart signs everybody out.
The alternative -- a `sessions.json` next to `accounts.json` -- buys
convenience and costs a second credential file on disk that has to be written
on almost every request, because the idle clock below moves when a request
arrives. It would also outlive the things it refers to: a session that
survives a restart points at approvals that no longer exist and turns that are
no longer running. The console is a tool you start; being signed out when you
restart it is the behaviour, not a limitation of it.

**The token is stored hashed even though the table is in memory**, which looks
like superstition until you ask where the table gets rendered. `GET
/api/auth/sessions` lists the caller's own sessions so that "sign out
everywhere" can exist, and a table of raw tokens is one careless
serialisation, one `repr()` in a traceback, one debug log line away from
handing out live credentials. Hashed, that mistake is not available.

The hash is a single SHA-256, not scrypt, and the difference is the whole
reason `accounts.py` uses scrypt: a password is low-entropy and guessable, so
verifying one must be made expensive on purpose. A session token is 32 random
bytes from `secrets`; there is nothing to guess, and this runs on every single
request.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field

#: The cookie's name. Prefixed because it is not the only cookie a browser on
#: this origin might carry, and unprefixed `session` is what every other tool
#: on `localhost:8000` also calls its cookie.
SESSION_COOKIE = "minicodex_session"

#: Twelve hours from sign-in, regardless of activity. The bound that cannot be
#: extended by using the session: a stolen cookie has an expiry that the thief
#: refreshing it cannot push out.
ABSOLUTE_TTL_SECONDS = 12 * 60 * 60

#: Two hours since the last request. The bound that closes a session somebody
#: walked away from. Both are needed -- idle alone never ends a session that is
#: being used by the wrong person, absolute alone signs out somebody who is in
#: the middle of working.
IDLE_TTL_SECONDS = 2 * 60 * 60


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Session:
    """One signed-in browser."""

    token_hash: str
    account_key: str
    created: float
    last_seen: float
    #: Free-text, for the "your sessions" list only. Truncated on the way in,
    #: because it is attacker-controlled and it is about to be rendered.
    user_agent: str = ""

    #: A stable public handle. The hash's first twelve characters: enough to
    #: name one session in a revoke request, and not enough to be one.
    @property
    def id(self) -> str:
        return self.token_hash[:12]

    def expired(self, now: float) -> bool:
        return now - self.created > ABSOLUTE_TTL_SECONDS or now - self.last_seen > IDLE_TTL_SECONDS

    def public(self, *, current: bool = False) -> dict[str, object]:
        return {
            "id": self.id,
            "created": self.created,
            "last_seen": self.last_seen,
            "user_agent": self.user_agent,
            "current": current,
        }


@dataclass
class SessionTable:
    """Every browser currently signed in to this process."""

    _by_hash: dict[str, Session] = field(default_factory=dict)

    # -- lifecycle -----------------------------------------------------------

    def create(self, account_key: str, *, user_agent: str = "") -> tuple[str, Session]:
        """Mint a token and return it exactly once.

        The raw token leaves this method and is never stored, so this is the
        only moment it exists anywhere except the browser's cookie jar. A
        `Set-Cookie` header is written from the first element; everything
        afterwards works from the second.
        """
        self.sweep()
        token = secrets.token_urlsafe(32)
        now = time.time()
        session = Session(
            token_hash=hash_token(token),
            account_key=account_key,
            created=now,
            last_seen=now,
            user_agent=user_agent[:200],
        )
        self._by_hash[session.token_hash] = session
        return token, session

    def touch(self, token: str) -> Session | None:
        """Look a cookie up and move the idle clock, or answer `None`.

        Every rejection returns the identical `None`: expired, revoked, never
        issued and typed by hand are one answer, for chapter 22's reason
        (F22-02) -- a route that distinguishes them tells a caller which
        tokens exist.
        """
        session = self._by_hash.get(hash_token(token))
        if session is None:
            return None
        now = time.time()
        if session.expired(now):
            # Drop it here rather than waiting for a sweep: this is the moment
            # the expiry is known, and leaving it in the table means the next
            # `sessions_for` call still lists it as active.
            self._by_hash.pop(session.token_hash, None)
            return None
        session.last_seen = now
        return session

    def revoke(self, token: str) -> bool:
        return self._by_hash.pop(hash_token(token), None) is not None

    def revoke_id(self, account_key: str, session_id: str) -> bool:
        """Revoke by public id, and only within one account.

        `session_id` arrives from a browser, so the account is *not* taken from
        it: the caller passes the account it already authenticated, and a
        session belonging to somebody else is simply not found. Without that
        pair, "sign out this device" is "sign out anybody's device".
        """
        for key, session in list(self._by_hash.items()):
            if session.id == session_id and session.account_key == account_key:
                del self._by_hash[key]
                return True
        return False

    def revoke_account(self, account_key: str) -> int:
        """Every session of one account. A password change ends all of them."""
        doomed = [k for k, s in self._by_hash.items() if s.account_key == account_key]
        for key in doomed:
            del self._by_hash[key]
        return len(doomed)

    # -- reading -------------------------------------------------------------

    def sessions_for(self, account_key: str) -> list[Session]:
        self.sweep()
        return sorted(
            (s for s in self._by_hash.values() if s.account_key == account_key),
            key=lambda s: s.created,
            reverse=True,
        )

    def sweep(self) -> int:
        now = time.time()
        doomed = [k for k, s in self._by_hash.items() if s.expired(now)]
        for key in doomed:
            del self._by_hash[key]
        return len(doomed)

    def __len__(self) -> int:
        return len(self._by_hash)


__all__ = [
    "ABSOLUTE_TTL_SECONDS",
    "IDLE_TTL_SECONDS",
    "SESSION_COOKIE",
    "Session",
    "SessionTable",
    "hash_token",
]
```

> - **两个时钟**：绝对 12 小时（偷来的 cookie 续不了命）、空闲 2 小时（离开的人自动退出）。
> - **`create`**：造一个随机令牌，只**返回**一次，表里存的是它的哈希。
> - **`touch`**：每个请求调一次：查到、没过期，就更新"最后一次见到"；过期了顺手删掉。
> - **`revoke_id`**：按公开的 id 撤销——**并且只在这个账号里找**（下面那段话）。
> - **`revoke_account`**：一个账号的全部会话（改口令、停用、"全部退出"）。

`session_id` 是浏览器送来的，所以账号**不从它身上取**：调用方传的是它自己
刚认证过的那个账号。少了这一对，"退出这台设备"就是"退出任何人的设备"。

测试：

```python
def _second_account(console: TestClient, key: str = "bob", **extra: Any) -> TestClient:
    """Create another account and return a client signed in as it.

    The client is *not* entered as a context manager, so each request runs on
    an event loop of its own that closes when the request does.  Fine for
    asking routes questions; wrong for anything that starts a turn, which is a
    background task and dies with that loop -- see
    `test_the_operators_key_is_not_sent_to_a_url_another_account_chose`."""
    response = console.post(
        "/api/accounts", json={"key": key, "password": SECOND_PASSWORD, **extra}
    )
    assert response.status_code == 200, response.text
    other = TestClient(console.app)
    signed = other.post("/api/auth/login", json={"key": key, "password": SECOND_PASSWORD})
    assert signed.status_code == 200, signed.text
    return other


def test_F23_04_another_account_cannot_read_your_thread(
    console: TestClient, workspace: Path
) -> None:
    """404, not 403, and the status code is the honest one.

    There is no ownership comparison anywhere in this path: `store_for` handed
    the route a store rooted at bob's own directory, and alice's thread is not
    in it. The thread is not forbidden to bob, it does not exist for him.
    """
    thread_id = _thread(console, workspace)
    bob = _second_account(console)
    assert bob.get(f"/api/threads/{thread_id}").status_code == 404
    assert bob.patch(f"/api/threads/{thread_id}", json={"title": "mine now"}).status_code == 404
    assert bob.delete(f"/api/threads/{thread_id}").json() == {"ok": False}
    assert bob.get("/api/workspaces").json() == []


def test_F23_04_another_account_cannot_open_your_event_socket(
    console: TestClient, workspace: Path
) -> None:
    """The socket is where the model's output and every command it ran go."""
    thread_id = _thread(console, workspace)
    bob = _second_account(console)
    with pytest.raises(Exception):  # noqa: B017
        with bob.websocket_connect(f"/ws/threads/{thread_id}"):
            pass


def test_F23_04_providers_and_mcp_servers_are_per_account(console: TestClient) -> None:
    console.post(
        "/api/mcp", json={"name": "mine", "command": "echo hi", "startup_timeout": 5.0}
    ).raise_for_status()
    bob = _second_account(console)
    assert [s["name"] for s in bob.get("/api/mcp").json()] == []
    # Providers are seeded per account, so bob has the two defaults and none of
    # alice's edits.
    alice_ids = {p["id"] for p in console.get("/api/providers").json()}
    bob_ids = {p["id"] for p in bob.get("/api/providers").json()}
    assert alice_ids and bob_ids and not (alice_ids & bob_ids)


def test_F23_04_an_approval_belongs_to_one_account(console: TestClient) -> None:
    """The brokers are separate objects, so there is no id to guess into.

    Twelve hex characters is not nothing, but "not enough entropy" is the wrong
    reason to be safe. There is no table containing anybody else's approvals.
    """
    live = console.app.state.console
    assert live.broker_for("alice") is not live.broker_for("bob")
    assert live.broker_for("alice") is live.broker_for("alice")


def test_F23_04_a_session_is_revoked_by_the_pair_not_by_the_id() -> None:
    """The one table that has to be shared, so the lookup takes both halves."""
    table = SessionTable()
    _token, alice = table.create("alice")
    _token2, _bob = table.create("bob")
    assert table.revoke_id("bob", alice.id) is False, "bob revoked alice's session"
    assert table.revoke_id("alice", alice.id) is True
    assert len(table) == 1


def test_F23_04_a_disabled_account_stops_being_signed_in(console: TestClient) -> None:
    """The session table outlives the account list, so it has to re-check.

    Revoking on `disable` is not enough on its own: a session created in the
    same millisecond, or a table that was never told, would still resolve. So
    `auth.resolve` looks the account up on every request and a disabled one is
    the same answer as a missing cookie.
    """
    bob = _second_account(console)
    assert bob.get("/api/workspaces").status_code == 200
    console.post("/api/accounts/bob/disable").raise_for_status()
    assert bob.get("/api/workspaces").status_code == 401


def test_F23_04_a_live_session_re_checks_the_account_on_every_request(
    console: TestClient,
) -> None:
    """Found by the mutation probe: nothing was testing this line.

    `POST /api/accounts/{key}/disable` revokes the account's sessions on the
    way out, so the disabled check inside `auth.resolve` never ran in any test
    -- breaking it changed no result. It is not redundant, though: the session
    table is in memory and the account list is a file, and anything that
    disables an account without going through that one route (a hand-edited
    `accounts.json`, a future admin tool, a second process) leaves a live
    session pointing at a disabled account.

    So this test disables it the way those would, behind the route, and asks
    whether the cookie still opens the door.
    """
    bob = _second_account(console)
    assert bob.get("/api/workspaces").status_code == 200

    live = console.app.state.console
    live.accounts.update("bob", disabled=True)  # no revoke, on purpose

    assert bob.get("/api/workspaces").status_code == 401
    assert live.sessions.sessions_for("bob") == [], "the stale session is dropped, not just refused"


def test_F23_04_an_admin_cannot_disable_themselves(console: TestClient) -> None:
    """A console with no enabled administrator has no way back."""
    response = console.post(f"/api/accounts/{CONSOLE_ACCOUNT}/disable")
    assert response.status_code == 400


def test_F23_04_a_normal_account_cannot_reach_the_admin_routes(console: TestClient) -> None:
    bob = _second_account(console)
    assert bob.get("/api/accounts").status_code == 403
    assert (
        bob.post("/api/accounts", json={"key": "eve", "password": SECOND_PASSWORD}).status_code
        == 403
    )
```

> - **`_second_account`**：建一个叫 `bob` 的账号，返回一个以他身份登录的客户端。说明里那段警告是这次改写加的（§18）。
> - 读不到别人的 thread（404）、连不上别人的事件 socket、模型来源和 MCP 服务器各是各的、审批属于一个账号。
> - `revoke_id`：用 alice 的账号去撤 bob 的会话 id，撤不掉。
> - 停用一个账号，它的会话立刻失效；直接改 `accounts.json` 把一个账号停用（**绕过**停用路由），下一个请求也会被拒——§18 讲这条测试是怎么来的。

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

修完以后的面板：

```python
@router.get("/api/threads/{thread_id}/memory")
def get_memory(request: Request, thread_id: str) -> dict[str, Any]:
    """The memory this thread's turns actually read (F23-05).

    For four chapters this route answered with `<workspace>/.minicodex/
    memories`, and no turn has ever read that directory: chapter 16 moved
    memory to the home directory, and `runtime.load_feature_dirs` has loaded it
    from there since chapter 19. So the panel showed an empty tab on a machine
    with a full memory file, and "forget everything" deleted nothing, and both
    were silent about it.

    `owner.memories()` is now the only spelling in this file, and `run_turn`
    takes the same `Owner`, which is what makes the two agree by construction
    rather than by both being edited on the same day.
    """
    auth = _auth(request)
    _console(request).require_thread(auth.owner, thread_id)
    directory = auth.owner.memories()
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
    auth = _auth(request)
    _console(request).require_thread(auth.owner, thread_id)
    path = auth.owner.jobs_db()
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
    auth = _auth(request)
    _console(request).require_thread(auth.owner, thread_id)
    directory = auth.owner.memories()
    removed = []
    for name in (BODY_FILE, SUMMARY_FILE, "usage.json"):
        target = directory / name
        if target.exists():
            target.unlink()
            removed.append(name)
    jobs_path = auth.owner.jobs_db()
    if jobs_path.exists():
        with JobStore(jobs_path) as jobs:
            jobs.forget_all()
    return {"removed": removed}
```

> 三个路由读的、删的都是 `auth.owner.memories()`——和一轮对话读的（`run_turn` 收到同一个 `Owner`）是同一个目录。

测试：

```python
def test_F23_05_the_memory_panel_reads_the_directory_the_agent_reads(
    console: TestClient, workspace: Path, tmp_path: Path
) -> None:
    """Written to the owner's home, read through the console. Same directory.

    Before this chapter the route answered with `<workspace>/.minicodex/
    memories` while every turn loaded `~/.minicodex/memories`, so this test
    would have shown an empty panel next to a full memory file -- which is
    exactly what a person would have seen.
    """
    memories = Owner(CONSOLE_ACCOUNT, home=tmp_path / "home").memories()
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("v1\n\n## thing\nbody\n", encoding="utf-8")

    thread_id = _thread(console, workspace, memory=True)
    payload = console.get(f"/api/threads/{thread_id}/memory").json()
    assert payload["directory"] == str(memories)
    assert [entry["title"] for entry in payload["entries"]] == ["thing"]


def test_F23_05_two_accounts_do_not_share_a_memory(
    console: TestClient, workspace: Path, tmp_path: Path
) -> None:
    """Chapter 20 named this leak and could not close it: there was nobody to
    ask who this was. `load_feature_dirs` now takes an `Owner` and the console
    has one."""
    alice_dir = Owner(CONSOLE_ACCOUNT, home=tmp_path / "home").memories()
    alice_dir.mkdir(parents=True)
    (alice_dir / "MEMORY.md").write_text("v1\n\n## alice's\nsecret\n", encoding="utf-8")

    thread_id = _thread(console, workspace)
    bob = _second_account(console)
    bob_thread = _thread(bob, workspace)

    bob_view = bob.get(f"/api/threads/{bob_thread}/memory").json()
    assert bob_view["directory"] != str(alice_dir)
    assert bob_view["entries"] == []
    alice_view = console.get(f"/api/threads/{thread_id}/memory").json()
    assert [e["title"] for e in alice_view["entries"]] == ["alice's"]


def test_F23_05_a_turn_loads_the_memory_of_the_owner_it_runs_as(tmp_path: Path) -> None:
    """The other half of F23-05, and the mutation probe found it missing.

    Every test above goes through a route, so all of them would still pass
    against a `load_feature_dirs` that ignored its `owner` argument and loaded
    the default owner's memory -- the panel would be right and the turn would
    be wrong, which is the exact shape of the bug this chapter is fixing, one
    function further in.
    """
    alice = Owner("alice", home=tmp_path / "home")
    bob = Owner("bob", home=tmp_path / "home")
    alice.memories().mkdir(parents=True)
    (alice.memories() / "MEMORY.md").write_text("v1\n\n## alice's\nsecret\n", encoding="utf-8")

    memory, remember, _skills, _notices = load_feature_dirs(
        tmp_path, {"memory": True, "remember": True}, alice
    )
    assert memory is not None and [e.title for e in memory.entries] == ["alice's"]
    assert remember == alice.memories()

    empty, bob_remember, _s, _n = load_feature_dirs(
        tmp_path, {"memory": True, "remember": True}, bob
    )
    assert empty is None, "bob must not load alice's memory"
    assert bob_remember == bob.memories()


def test_F23_05_the_skills_panel_was_right_all_along(console: TestClient, workspace: Path) -> None:
    """The two panels were written side by side with the same path shape.

    Skills stayed per-project when memory moved to the home directory, so the
    workspace-relative spelling is correct here and was wrong one function
    above it. The pair is the whole lesson: a shape that is right next door is
    not evidence.
    """
    skills = workspace / ".minicodex" / "skills"
    skills.mkdir(parents=True)
    thread_id = _thread(console, workspace)
    payload = console.get(f"/api/threads/{thread_id}/skills").json()
    assert payload["directory"] == str(skills)
```

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


量一下。第一版在它的 Windows 机器上量的（五次平均）：

```
               known account   no such account
without the dummy hash   145.3 ms      0.148 ms
as shipped               142.6 ms    178.548 ms
```

这次改写在 Linux 上重量了一遍（`timing23.py`：同一个 `AccountStore`，一个存在的账号、一个不存在的账号，各五次取平均；"without the dummy hash" 是把 `verify` 前半段照第一版的写法重写的一个函数）：

```
                          known account   no such account
without the dummy hash          34.3 ms          0.036 ms
as shipped                      34.2 ms         34.008 ms
```

scrypt 在这台机器上快一些（34 毫秒对 145 毫秒），比例是同一个量级：**差了大约一千倍**。

修法是：查不到的时候，拿口令去校验一个谁也不知道的哈希，然后把结果扔掉。


```python
def verify(self, key: str, password: str) -> Account | None:
    """The one place a password is checked.

    The hash comparison inside `verify_password` is constant time, and on
    its own that buys nothing here: with a plain `if account is None:
    return None` in front of it, an unknown name answers in microseconds
    and a known name answers in the tens of milliseconds scrypt costs, so
    the *timing of the miss* enumerates the account list. Which is worse
    than it sounds for this console, because an account key is also a
    directory name and a tenancy.

    So a miss verifies the password against a hash of something nobody
    knows and throws the answer away. Every login now costs one scrypt run,
    which is exactly the property being paid for.
    """
    account = self.get(key.strip().lower())
    if account is None:
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, account.password_hash):
        return None
    if account.disabled:
        return None
    return account
```

现在每一次登录都花一次 scrypt——而这正是要买的那个性质。

路由那一层跟着来：账号不存在、口令不对、账号被停用，**同一个 401、同一句话**。
说"没有这个账号"会把上面那笔钱白花。

`login` 路由：

```python
class Credentials(BaseModel):
    key: str
    password: str


class Bootstrap(Credentials):
    token: str


class NewAccount(Credentials):
    admin: bool = False
    workspace_roots: list[str] = []


class NewPassword(BaseModel):
    current: str
    replacement: str


def _sign_in(request: Request, account: Any) -> JSONResponse:
    """Mint a session and put it in a cookie. The one place that happens."""
    console = _console(request)
    token, _session = console.sessions.create(
        account.key, user_agent=request.headers.get("user-agent", "")
    )
    response = JSONResponse({"account": account.public()})
    _set_cookie(response, token, secure=request.url.scheme == "https")
    return response


@router.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, Any]:
    """Public, and it says as little as it can get away with.

    The frontend has exactly one decision to make before anybody is signed in:
    render the bootstrap form or the login form. So this answers that -- has
    this console got an account yet -- and whether the caller is already
    signed in. It does not list accounts, and `has_account` is a boolean
    rather than a count, because "how many people use this server" is not
    something an anonymous caller has any business learning.

    It resolves the session by hand because it is on `auth.PUBLIC_PATHS`: the
    gate skipped this request, so `request.state.auth` is not set. That is the
    cost of a public route on a deny-by-default server, and it is a cost worth
    seeing rather than hiding behind a helper that works either way.
    """
    console = _console(request)
    auth = resolve_auth(
        request.scope.get("headers") or [], console.accounts, console.sessions, console.home
    )
    if auth is None:
        return {"has_account": not console.accounts.empty(), "signed_in": False, "account": None}
    return {
        "has_account": True,
        "signed_in": True,
        "account": auth.account.public(),
        "quota": console.quota.snapshot(
            auth.account.key,
            auth.account.quota_limits(),
            running=console.running_for(auth.account.key),
        ),
    }


@router.post("/api/auth/bootstrap")
def bootstrap(request: Request, body: Bootstrap) -> JSONResponse:
    """Redeem the token the server printed and become the first account."""
    console = _console(request)
    try:
        account = console.accounts.redeem_bootstrap(body.token, body.key, body.password)
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    moved = adopt_legacy_records(console.data_dir, account.key)
    if moved:
        log.info("adopted pre-chapter-23 records into %s: %s", account.key, ", ".join(moved))
    return _sign_in(request, account)


@router.post("/api/auth/login")
def login(request: Request, body: Credentials) -> JSONResponse:
    """One message for every way this can fail.

    Unknown account, wrong password and disabled account are the same 401 with
    the same text. `AccountStore.verify` has already made them cost the same
    number of milliseconds; saying "no such account" here would give back for
    free what that was paying for.
    """
    console = _console(request)
    account = console.accounts.verify(body.key, body.password)
    if account is None:
        raise HTTPException(401, "that account name and password do not match")
    return _sign_in(request, account)
```

> - **`_sign_in`**：造一个会话，把令牌放进 cookie（`set_session_cookie`，§15）。登录、bootstrap、改口令都走它。
> - **`auth_status`**：公开的，所以 `request.state.auth` 没有——它自己 `resolve` 一次。
> - **`bootstrap`**：用令牌建第一个账号，然后收养旧记录（§15），然后登录。
> - **`login`**：`verify` 返回 `None` 就是同一句 401。

测试：

```python
def test_F23_06_an_unknown_account_costs_the_same_as_a_wrong_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured, with the real cost parameter put back for this one test.

    The suite runs at `SCRYPT_N = 2**4` so that signing in is free (see
    `conftest.cheap_password_hashing`); at that cost the difference this test
    is about is inside the noise. Here the production figure is restored, one
    account is created, and the two failure paths are timed.

    The assertion is a ratio rather than an absolute, because absolutes in a
    timing test are how a suite becomes flaky on somebody else's laptop. A
    miss that skipped the hash entirely comes in around a hundred times faster;
    anything inside 3x is the same code path doing the same work.
    """
    monkeypatch.setattr(accounts_mod, "SCRYPT_N", accounts_mod.SCRYPT_N.__class__(2**14))
    store = AccountStore(tmp_path / "accounts.json")
    store.create("alice", "a-real-password-here")

    def timed(key: str) -> float:
        start = time.perf_counter()
        assert store.verify(key, "definitely-not-the-password") is None
        return time.perf_counter() - start

    known = timed("alice")
    unknown = timed("nobody-by-that-name")
    ratio = max(known, unknown) / max(min(known, unknown), 1e-9)
    assert ratio < 3.0, f"known {known * 1000:.0f} ms vs unknown {unknown * 1000:.0f} ms"


def test_F23_06_the_login_route_says_the_same_thing_either_way(anon: TestClient) -> None:
    sign_in(anon)
    wrong = anon.post(
        "/api/auth/login", json={"key": CONSOLE_ACCOUNT, "password": "nope-nope-nope"}
    )
    missing = anon.post("/api/auth/login", json={"key": "ghost", "password": "nope-nope-nope"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()


def test_F23_06_a_disabled_account_is_not_a_distinguishable_answer(console: TestClient) -> None:
    _second_account(console)
    console.post("/api/accounts/bob/disable").raise_for_status()
    third = TestClient(console.app)
    refused = third.post("/api/auth/login", json={"key": "bob", "password": SECOND_PASSWORD})
    ghost = third.post("/api/auth/login", json={"key": "ghost", "password": SECOND_PASSWORD})
    assert refused.status_code == 401
    assert refused.json() == ghost.json()
```

> 第一个测试量的是同一个函数的两次调用的耗时比值，不是绝对毫秒——这样在快机器、慢机器上都成立。

## 11. F23-07：撤销了访问，没有停下工作

一个回合是一个**活过了发起它的那个请求**的后台任务。

所以"在所有设备上退出登录"如果只清空会话表，结果是：agent 还在替一个**一条
有效凭证都不剩**的账号改文件、跑命令——而且没有人在看那条 socket，这让它更糟
而不是更好。


```python
def _revoke_everything(console: Any, account_key: str) -> dict[str, int]:
    """Sign an account out everywhere, and stop what it left running.

    Revoking the sessions alone is the version that looks finished and is not:
    a turn is a background task that outlived the request which started it, so
    after "sign out everywhere" the agent carries on editing files and running
    commands on behalf of an account that no longer has a single valid
    credential. Nobody is watching the socket, which is exactly why it is
    worse rather than better.

    Cancelling is the same call the stop button makes, so the turn ends the way
    chapter 7 promises: `mark("interrupted")`, a resumable session file.
    """
    sessions = console.sessions.revoke_account(account_key)
    prefix = f"{account_key}/"
    cancelled = 0
    for key, task in list(console.running.items()):
        if key.startswith(prefix) and not task.done():
            task.cancel()
            cancelled += 1
    console.broker_for(account_key).fail_all("signed out")
    return {"sessions": sessions, "turns": cancelled}


@router.post("/api/auth/logout")
def logout(request: Request) -> JSONResponse:
    auth = _auth(request)
    _console(request).sessions.revoke(auth.token)
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@router.get("/api/auth/sessions")
def list_sessions_for_me(request: Request) -> list[dict[str, Any]]:
    auth = _auth(request)
    console = _console(request)
    return [
        session.public(current=session.id == auth.session.id)
        for session in console.sessions.sessions_for(auth.account.key)
    ]


@router.delete("/api/auth/sessions/{session_id}")
def revoke_one_session(request: Request, session_id: str) -> dict[str, bool]:
    auth = _auth(request)
    if not _console(request).sessions.revoke_id(auth.account.key, session_id):
        raise HTTPException(404, "no such session")
    return {"ok": True}


@router.post("/api/auth/sessions/revoke-all")
def revoke_all_sessions(request: Request) -> JSONResponse:
    auth = _auth(request)
    counts = _revoke_everything(_console(request), auth.account.key)
    response = JSONResponse(counts)
    clear_session_cookie(response)
    return response


@router.post("/api/auth/password")
def change_password(request: Request, body: NewPassword) -> JSONResponse:
    """Change it, then end every session including this one, then sign in.

    A password change that leaves the old sessions alive is a password change
    that does not evict whoever the change was *for*. So: revoke everything,
    mint one new session for the browser that asked, and let every other device
    log in again with the new password.
    """
    auth = _auth(request)
    console = _console(request)
    if console.accounts.verify(auth.account.key, body.current) is None:
        raise HTTPException(403, "the current password does not match")
    if len(body.replacement) < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"a password needs at least {MIN_PASSWORD_LENGTH} characters")
    try:
        console.accounts.set_password(auth.account.key, body.replacement)
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    _revoke_everything(console, auth.account.key)
    account = console.accounts.get(auth.account.key)
    return _sign_in(request, account)
```

> - **`_revoke_everything`**：撤销这个账号的全部会话；`running` 里以 `<key>/` 开头的回合全部 `cancel()`；这个账号的审批代理里还在等的审批全部失败。返回撤了几个会话、停了几个回合。
> - **`revoke_all_sessions`** / **`change_password`** / 管理员的 **`disable_account`**（下面）都调它。
> - **`change_password`**：先用旧口令 `verify`；新口令太短 400；改完撤销全部，再给发起请求的这个浏览器发一个新会话。

退出全部、改口令、停用账号，三条路都调它。

`task.cancel()` 是停止按钮调的同一个调用，所以回合按第 7 章承诺的方式结束：
`mark("interrupted")`，一个可恢复的会话文件。

**改口令这件事也是同一个道理。** 一次不把旧会话赶出去的改口令，没有赶走它
本来要赶走的那个人。所以：撤销全部，给发起请求的这个浏览器发一个新会话，其他
每一台设备拿新口令重新登。

管理员的那几个路由：

```python
def _admin(request: Request) -> AuthContext:
    auth = _auth(request)
    if not auth.account.admin:
        raise HTTPException(403, "this needs an administrator account")
    return auth


@router.get("/api/accounts")
def list_accounts(request: Request) -> list[dict[str, Any]]:
    _admin(request)
    return [a.public() for a in _console(request).accounts.all()]


@router.post("/api/accounts")
def create_account(request: Request, body: NewAccount) -> dict[str, Any]:
    _admin(request)
    roots = tuple(str(Path(r).expanduser().resolve()) for r in body.workspace_roots)
    try:
        account = _console(request).accounts.create(
            body.key, body.password, admin=body.admin, workspace_roots=roots
        )
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    return account.public()


@router.post("/api/accounts/{key}/disable")
def disable_account(request: Request, key: str) -> dict[str, Any]:
    """Disable, never delete.

    There is no delete route, and the absence is deliberate: an account key is
    a directory name under two different trees, one of which holds OAuth
    tokens for other people's servers. Removing the row while the directories
    stand would let the same key be handed out again and inherit them. A
    disabled account cannot sign in, its live sessions are revoked here, and
    what it owns stays where it is until somebody decides on purpose what to
    do with it.
    """
    admin = _admin(request)
    if key == admin.account.key:
        raise HTTPException(400, "an administrator cannot disable their own account")
    console = _console(request)
    account = console.accounts.update(key, disabled=True)
    if account is None:
        raise HTTPException(404, f"no account {key!r}")
    _revoke_everything(console, key)
    return account.public()
```

测试：

```python
def test_F23_07_signing_out_everywhere_cancels_this_accounts_turns(
    console: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn is a background task that outlived the request that started it.

    So "sign out everywhere" that only empties the session table leaves the
    agent editing files and running commands for an account with no valid
    credential left, with nobody watching the socket.
    """
    import asyncio

    from minicodex.web import routes

    async def never(**_kwargs: Any) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(routes, "run_turn", never)

    thread_id = _thread(console, workspace)
    provider = console.get("/api/providers").json()[0]
    console.post(f"/api/providers/{provider['id']}/activate")
    assert (
        console.post(f"/api/threads/{thread_id}/messages", json={"text": "go"}).status_code == 202
    )

    live = console.app.state.console
    key = turn_key(CONSOLE_ACCOUNT, thread_id)
    assert key in live.running
    # Held before the revoke: the turn's own `finally` pops it out of
    # `running`, so reading the table afterwards is a race with the cleanup
    # that is supposed to happen.
    task = live.running[key]

    counts = console.post("/api/auth/sessions/revoke-all").json()
    assert counts["sessions"] == 1
    assert counts["turns"] == 1
    assert task.cancelled() or task.cancelling()
    assert console.get("/api/workspaces").status_code == 401


def test_F23_07_disabling_an_account_revokes_its_sessions(console: TestClient) -> None:
    bob = _second_account(console)
    live = console.app.state.console
    assert len(live.sessions.sessions_for("bob")) == 1
    console.post("/api/accounts/bob/disable").raise_for_status()
    assert live.sessions.sessions_for("bob") == []
    assert bob.get("/api/workspaces").status_code == 401


def test_F23_07_changing_a_password_ends_every_other_session(console: TestClient) -> None:
    """Otherwise a password change does not evict whoever it was for."""
    second_browser = TestClient(console.app)
    second_browser.post(
        "/api/auth/login", json={"key": CONSOLE_ACCOUNT, "password": CONSOLE_PASSWORD}
    ).raise_for_status()
    assert second_browser.get("/api/workspaces").status_code == 200

    changed = console.post(
        "/api/auth/password",
        json={"current": CONSOLE_PASSWORD, "replacement": "a-brand-new-password"},
    )
    assert changed.status_code == 200
    assert second_browser.get("/api/workspaces").status_code == 401
    # And the browser that asked keeps working, on a new session.
    assert console.get("/api/workspaces").status_code == 200


def test_F23_07_a_wrong_current_password_changes_nothing(console: TestClient) -> None:
    response = console.post(
        "/api/auth/password", json={"current": "not-it-at-all", "replacement": "a-new-password-x"}
    )
    assert response.status_code == 403
    assert console.get("/api/workspaces").status_code == 200


def test_F23_07_logging_out_ends_exactly_one_session(console: TestClient) -> None:
    second_browser = TestClient(console.app)
    second_browser.post(
        "/api/auth/login", json={"key": CONSOLE_ACCOUNT, "password": CONSOLE_PASSWORD}
    ).raise_for_status()
    console.post("/api/auth/logout").raise_for_status()
    assert console.get("/api/workspaces").status_code == 401
    assert second_browser.get("/api/workspaces").status_code == 200
```

> 第一个：把 `run_turn` 换成一个睡一小时的假函数，开一轮；"全部退出"之后，断言撤了 1 个会话、停了 1 个回合，那个任务已经被取消，这个客户端再请求是 401。

## 12. F23-08：配额在**认领**时收费

在完成时收费，看上去更公平，其实是错的：**一个失败的回合花掉的模型调用、
子进程和秒数，和成功的一模一样。** 只算成功的配额，是一个重试循环不需要遵守
的配额。

所以收费点在路由里那一段同步代码里，紧挨着第 19 章 F19-01 已经放在那儿的
`busy` 认领：


`send_message` 里的那一段：

```python
# And the quota in the same synchronous stretch, for the same reason: two
# requests that both awaited before charging would both be charged after
# both had started. `running_for` counts `busy`, which this handler has not
# added to yet, so the count is of turns other than this one.
try:
    console.quota.claim(
        auth.account.key,
        auth.account.quota_limits(),
        running=console.running_for(auth.account.key),
    )
except QuotaExceeded as exc:
    raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)}) from exc
console.busy.add(key)

provider = store.active_provider()
if provider is None:
    console.busy.discard(key)
    # Refunded: nothing ran. A 400 for a console that was never configured
    # must not spend the hour it would have taken to answer.
    console.quota.refund(auth.account.key)
    raise HTTPException(400, "no active model provider -- add one under Extensions")
```

`quota.py` 全文：

```python
"""How much of this server one account may use at once, and per hour.

A quota is not a security control -- the gate in `auth.py` is -- and it is not
billing either. It is the answer to a smaller question that only appears once a
server has more than one user: **whose turn gets to run when they both want
one.** Without it, a single account can start a turn on every thread it owns
and hold the process's event loop, its subprocesses and its model budget for
as long as it likes, and the second person's first experience of the console
is that it is slow for reasons nobody can see.

**Both limits are charged at claim time, before any work happens.** Charging
on completion is the mistake that looks like fairness: a turn that fails costs
the same model call, the same subprocess and the same seconds as one that
succeeds, so a quota that only counts successes is a quota a retry loop does
not have. `routes.send_message` already claims `busy` synchronously in the
handler for the same class of reason (F19-01), and this claim goes in the same
place, next to it.

**Turns, not tokens, and that is a scope decision rather than an oversight.**
A token or cost quota is what a hosted product would want, and this program
does not have the number it would need: `model.py` does parse the server's
reported `prompt_tokens` / `completion_tokens`, but the only thing that
consumes them is `tokens.Calibration`, which keeps a *ratio* and throws the
totals away. Metering somebody against `Calibration.correct()` -- an estimate
that is documented as useless for the first two turns of a session -- would be
a number with a decimal point and no defence. Counting turns is exact, is
known at the only moment enforcement can happen, and does not require pushing
a billing concern down into the agent.

**In memory, like the session table**, and for the same reason: this process
owns what it is currently doing. A restart forgives the last hour, which is
the wrong behaviour for a bill and an acceptable one for a fairness device on
a tool you can restart yourself.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

#: How many turns one account may have in flight. Two rather than one because
#: reviewing a finished thread while another one works is the normal way to use
#: this console, and rather than "as many as you like" because each in-flight
#: turn is a model connection, a rollout writer and possibly a subprocess.
DEFAULT_CONCURRENT_TURNS = 2

#: And how many it may start per hour. Sized against the slowest useful thing:
#: sixty turns an hour is one a minute, sustained, which no person types and a
#: runaway frontend reaches in seconds.
DEFAULT_TURNS_PER_HOUR = 60

WINDOW_SECONDS = 60 * 60


@dataclass(frozen=True)
class Limits:
    """One account's allowance. Per account, so it can be raised for one.

    Stored on the account record rather than in this module, so that "give
    alice more" is a change to `accounts.json` and not a redeploy -- and so
    that the defaults above are what a *new* account gets, not what every
    account is stuck with.
    """

    concurrent_turns: int = DEFAULT_CONCURRENT_TURNS
    turns_per_hour: int = DEFAULT_TURNS_PER_HOUR

    @classmethod
    def merge(cls, raw: dict[str, int] | None) -> Limits:
        raw = raw or {}
        return cls(
            concurrent_turns=int(raw.get("concurrent_turns", DEFAULT_CONCURRENT_TURNS)),
            turns_per_hour=int(raw.get("turns_per_hour", DEFAULT_TURNS_PER_HOUR)),
        )


class QuotaExceeded(Exception):
    """Refused, with the two things the caller actually needs.

    `retry_after` is seconds and is a real number rather than a constant: for
    the hourly window it is the age of the oldest turn still in it, so a
    frontend that waits exactly that long succeeds exactly once.
    """

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class QuotaLedger:
    """When each account last started a turn. One deque per account."""

    _starts: dict[str, deque[float]] = field(default_factory=dict)

    def _window(self, account_key: str, now: float) -> deque[float]:
        starts = self._starts.setdefault(account_key, deque())
        # Swept here rather than on a timer, chapter 22's trade (F22-03): the
        # only moment the answer matters is the moment somebody asks.
        while starts and now - starts[0] > WINDOW_SECONDS:
            starts.popleft()
        return starts

    def claim(self, account_key: str, limits: Limits, *, running: int) -> None:
        """Charge one turn, or raise.

        `running` is passed in rather than counted here, and that is the seam
        that keeps this class testable: the console's `busy` set is keyed by
        thread and this ledger has never heard of threads. The caller -- which
        does know which threads are this account's -- does the intersection.
        """
        if running >= limits.concurrent_turns:
            raise QuotaExceeded(
                f"you already have {running} turn(s) running "
                f"(limit {limits.concurrent_turns}); wait for one to finish",
                # No useful number: it ends when a turn ends, and this class
                # does not know how long that is. Ten seconds is a polling
                # interval, offered as such.
                retry_after=10,
            )
        now = time.time()
        starts = self._window(account_key, now)
        if len(starts) >= limits.turns_per_hour:
            oldest = starts[0]
            raise QuotaExceeded(
                f"you have started {len(starts)} turns in the last hour "
                f"(limit {limits.turns_per_hour})",
                retry_after=max(1, int(WINDOW_SECONDS - (now - oldest)) + 1),
            )
        starts.append(now)

    def refund(self, account_key: str) -> None:
        """Undo the most recent claim, for a turn that never started.

        Exactly one caller: `send_message` claims the quota and then discovers
        the thread has no provider configured, or the store rejects it. That is
        not a turn -- nothing was spent -- and leaving the claim would let a
        misconfigured console spend somebody's hour on 400s.

        A turn that *starts* and then fails is not refunded, which is the
        distinction the docstring at the top of this file is about.
        """
        starts = self._starts.get(account_key)
        if starts:
            starts.pop()

    def snapshot(self, account_key: str, limits: Limits, *, running: int) -> dict[str, int]:
        """What the console shows next to the account name."""
        starts = self._window(account_key, time.time())
        return {
            "running": running,
            "concurrent_limit": limits.concurrent_turns,
            "used_this_hour": len(starts),
            "hourly_limit": limits.turns_per_hour,
        }


__all__ = [
    "DEFAULT_CONCURRENT_TURNS",
    "DEFAULT_TURNS_PER_HOUR",
    "WINDOW_SECONDS",
    "Limits",
    "QuotaExceeded",
    "QuotaLedger",
]
```

> - **`Limits`**：每个账号两个数：同时在跑的回合数（默认 2）、一小时内能开始的回合数（默认 60）。存在账号记录里，可以给某个人单独调。
> - **`QuotaLedger._window`**：每个账号一个 `deque`，记着每一轮开始的时刻；超过一小时的从左边弹掉（懒清扫，第 22 章 §5 的做法）。
> - **`claim`**：同时在跑的够了 → 拒绝（`retry_after=10`，一个轮询间隔）；一小时满了 → 拒绝（`retry_after` 是最老那一轮还要多久才出窗口）；否则记一笔。
> - **`refund`**：只有一个调用者——没配模型来源的那个 400。
> - **`snapshot`**：给界面显示用。

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

测试：

```python
def test_F23_08_a_turn_is_charged_before_it_runs() -> None:
    ledger = QuotaLedger()
    limits = Limits(concurrent_turns=99, turns_per_hour=2)
    ledger.claim("alice", limits, running=0)
    ledger.claim("alice", limits, running=0)
    with pytest.raises(QuotaExceeded):
        ledger.claim("alice", limits, running=0)


def test_F23_08_a_failing_turn_is_not_free() -> None:
    """The distinction the whole design turns on.

    Charging on completion means a turn that raises costs nothing, so a retry
    loop against a broken provider is unmetered -- while consuming exactly the
    model calls and subprocesses a successful turn would have.
    """
    ledger = QuotaLedger()
    limits = Limits(turns_per_hour=1)
    ledger.claim("alice", limits, running=0)
    # Nothing reports success or failure to the ledger. There is no such method.
    assert not hasattr(ledger, "complete")
    with pytest.raises(QuotaExceeded):
        ledger.claim("alice", limits, running=0)


def test_F23_08_a_turn_that_never_started_is_refunded() -> None:
    ledger = QuotaLedger()
    limits = Limits(turns_per_hour=1)
    ledger.claim("alice", limits, running=0)
    ledger.refund("alice")
    ledger.claim("alice", limits, running=0)  # the hour is free again


def test_F23_08_the_concurrency_limit_counts_only_this_account() -> None:
    ledger = QuotaLedger()
    limits = Limits(concurrent_turns=1)
    ledger.claim("alice", limits, running=0)
    # bob has one running; alice's own count is what her limit is about.
    ledger.claim("bob", limits, running=0)
    with pytest.raises(QuotaExceeded) as raised:
        ledger.claim("alice", limits, running=1)
    assert raised.value.retry_after > 0


def test_F23_08_the_route_answers_429_with_a_retry_after(
    console: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from minicodex.web import routes

    async def never(**_kwargs: Any) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(routes, "run_turn", never)
    live = console.app.state.console
    monkeypatch.setattr(
        type(live.accounts.get(CONSOLE_ACCOUNT)),
        "quota_limits",
        lambda self: Limits(concurrent_turns=1, turns_per_hour=99),
    )

    provider = console.get("/api/providers").json()[0]
    console.post(f"/api/providers/{provider['id']}/activate")
    first = _thread(console, workspace)
    second = _thread(console, workspace)
    assert console.post(f"/api/threads/{first}/messages", json={"text": "a"}).status_code == 202
    refused = console.post(f"/api/threads/{second}/messages", json={"text": "b"})
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0


def test_F23_08_a_console_with_no_provider_does_not_spend_the_hour(
    console: TestClient, workspace: Path
) -> None:
    """No provider is a 400 and nothing ran, so nothing is charged."""
    thread_id = _thread(console, workspace)
    before = console.get("/api/auth/status").json()["quota"]["used_this_hour"]
    assert console.post(f"/api/threads/{thread_id}/messages", json={"text": "a"}).status_code == 400
    after = console.get("/api/auth/status").json()["quota"]["used_this_hour"]
    assert after == before
```

---

## 13. F23-09：workspace 是这一章唯一没被 key 罩住的东西

这一章分开的每一样东西，都住在一个 key 底下：记录、token、记忆、审批。

**workspace 不是。** 它是服务器上的任意一个绝对路径，而被指向它的那个东西，
整个存在的意义就是在那儿读文件、跑命令。

两个 workspace 不受限的账号，就是两个可以各自在对方 home 目录上开 thread 的
账号——底下的租户切得再干净也没用。


```python
def _validated_workspace(raw: str, account: Any) -> str:
    """An absolute path that exists, and that this account may open.

    `run_turn` does `root.mkdir(parents=True, exist_ok=True)` because the agent
    has to be able to work in a directory that is being created around it. That
    is the wrong behaviour for *this* string, which is a person typing a path
    into a browser: a typo used to silently create a directory tree anywhere
    the server user could write, and then run an agent inside it.

    The last check is chapter 23's, and it is the one that stops the rest of
    the chapter from being decorative. Everything else this chapter separated
    -- records, tokens, memories, approvals -- lives under a key. A workspace
    does not: it is any absolute path on the server, and the entire purpose of
    the thing being pointed at it is to read files and run commands there. Two
    accounts with unrestricted workspaces are two accounts that can each open a
    thread on the other's home directory, and no amount of tenancy underneath
    changes that.

    Empty `workspace_roots` still means unrestricted, because that is what a
    one-operator install is and this console is still normally that. It becomes
    a decision the operator makes when they create the second account, which is
    why `POST /api/accounts` takes the roots and why they are shown on the
    account page rather than kept in a config file.
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
    resolved = path.resolve()
    roots = getattr(account, "workspace_roots", ()) or ()
    if roots and not any(_within(resolved, Path(root)) for root in roots):
        raise HTTPException(
            403,
            f"{resolved} is outside the directories this account may use: " + ", ".join(roots),
        )
    return str(resolved)


def _within(path: Path, root: Path) -> bool:
    """Is `path` inside `root`, with both resolved first.

    `Path.is_relative_to` rather than a string prefix, which says yes to
    `/srv/data-of-someone-else` for the root `/srv/data`. Both sides are
    resolved before the comparison, so a symlink pointing out of the root is
    followed first rather than after -- chapter 20 (F20-06) learned the same
    thing about a sandbox's writable roots.
    """
    try:
        return path.resolve().is_relative_to(root.expanduser().resolve())
    except (OSError, ValueError):
        return False
```

`_within` 用 `Path.is_relative_to`，**不是字符串前缀**——前缀会对根目录
`/srv/data` 放行 `/srv/data-of-someone-else`。两边都先 `resolve()`，所以一个
指向根外面的符号链接是在比较**之前**被跟随的，不是之后。第 20 章 F20-06 在
沙箱的可写根上学过同一件事，这是它上移了一层。

**空的 `workspace_roots` 仍然表示不受限**，因为那正是一个单人安装的样子，而
这个控制台通常仍然是那个。它在创建第二个账号的时候变成一个决定——所以创建
表单问它，账号页显示它，而不是把它藏进一个配置文件。

建 thread 的路由，和给前端列出可选目录的那个：

```python
@router.get("/api/workspaces")
def get_workspaces(request: Request) -> list[dict[str, Any]]:
    console = _console(request)
    auth = _auth(request)
    store = console.store_for(auth.owner)
    groups = store.workspaces()
    for group in groups:
        for thread in group["threads"]:
            sessions = list_sessions(store.thread_dir(thread["id"]))
            thread["message_count"] = len(sessions[0].items) if sessions else 0
            thread["model"] = sessions[0].meta.model if sessions else None
            thread["busy"] = turn_key(auth.account.key, thread["id"]) in console.busy
    return groups


@router.post("/api/threads")
def create_thread(request: Request, body: NewThread) -> dict[str, Any]:
    auth = _auth(request)
    workspace = _validated_workspace(body.workspace, auth.account)
    try:
        settings = validate_settings(body.settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    store = _console(request).store_for(auth.owner)
    return store.new_thread(workspace, body.title.strip() or "New session", settings)
```

测试：

```python
def test_F23_09_an_account_with_roots_cannot_open_a_thread_outside_them(
    console: TestClient, tmp_path: Path
) -> None:
    """Everything else this chapter separates lives under a key. This does not.

    A workspace is a path on the server, and the point of the thing being
    pointed at it is to read files and run commands there.
    """
    allowed = tmp_path / "allowed"
    (allowed / "project").mkdir(parents=True)
    forbidden = tmp_path / "somebody-else"
    forbidden.mkdir()

    bob = _second_account(console, workspace_roots=[str(allowed)])
    ok = bob.post("/api/threads", json={"workspace": str(allowed / "project"), "title": "t"})
    assert ok.status_code == 200
    refused = bob.post("/api/threads", json={"workspace": str(forbidden), "title": "t"})
    assert refused.status_code == 403
    assert str(forbidden) in refused.json()["detail"]


def test_F23_09_a_prefix_that_is_not_a_parent_is_not_inside(
    console: TestClient, tmp_path: Path
) -> None:
    """`/srv/data` must not admit `/srv/data-of-someone-else`.

    A string prefix says yes to that. `Path.is_relative_to` says no, which is
    why the comparison is on paths and not on text.
    """
    root = tmp_path / "data"
    root.mkdir()
    sibling = tmp_path / "data-of-someone-else"
    sibling.mkdir()
    bob = _second_account(console, workspace_roots=[str(root)])
    assert (
        bob.post("/api/threads", json={"workspace": str(sibling), "title": "t"}).status_code == 403
    )


def test_F23_09_no_roots_still_means_unrestricted(console: TestClient, tmp_path: Path) -> None:
    """What a one-operator install is, and this console is still normally that.

    Stated as a test rather than left implicit, because it is the sentence
    somebody will want to check before deciding whether their own deployment is
    safe: an account created without roots may open any directory the server
    process can.
    """
    anywhere = tmp_path / "anywhere"
    anywhere.mkdir()
    assert (
        console.post("/api/threads", json={"workspace": str(anywhere), "title": "t"}).status_code
        == 200
    )
```

### 13.1 改写时发现：这一章的"多租户"不是一条安全边界

`_validated_workspace` 的说明说，这个检查是"让这一章其余部分不至于只是装饰的那一个"。它限制的是什么？thread 的**工作目录**——`read_file`、`apply_patch` 只能碰这个目录下的文件，shell 从这里**开始**。

它没有限制的，是 shell 命令能碰什么。第 20 章的沙箱在第 23 步里没有接上（第 20 章 §0.4），所以 agent 的每一条 shell 命令都以**运行控制台的那个系统用户**的身份执行。而审批——第 5 章那道"要不要执行这条命令"的关卡——是**由这个账号自己点的**。一个账号想在这台服务器上执行什么，只需要让模型提出来，然后自己点"允许"。或者更直接：添加一个 stdio MCP 服务器，命令随便写（第 22 章的表单），下一轮开始时它就被执行了。

这意味着什么，量一下最直接的那一件。第 2 章的 shell 只把一小份环境变量传给子进程（F02-09 的白名单），所以 `$OPENAI_API_KEY` 在 shell 里是空的。可子进程能读**父进程的**环境：

```
$ OPENAI_API_KEY=sk-operators-own-key uv run python -c "...ShellSession(...).run(...)..."
env  : []
/proc: OPENAI_API_KEY=sk-operators-own-key
```

（一个 `ShellSession`，先 `echo $OPENAI_API_KEY`，再读 `/proc/$PPID/environ`。）

同一个道理，`tenants/` 下面别的账号的 `providers.json`（明文 API key）、`~/.minicodex/tenants/` 下面别人的 `mcp_tokens.json`、`accounts.json` 里的口令哈希——全都是**同一个系统用户**的文件。§3.2 把它们改成了 0600，那挡住的是**这台机器上的其他系统用户**，挡不住控制台自己的账号：它们的命令就是以文件主人的身份跑的。

所以这一章做到的是：**在控制台的 API 里**，每个账号只看得到、改得了自己的记录；路由不会把 bob 的 thread 交给 alice。它**没有**做到的是：一个账号不能通过它的 agent 去读 bob 的东西、或者读运营者的密钥。第一版没有说这一点，而它是一个运营者决定"给谁开账号"时最需要知道的事：

> **这个控制台的每一个账号，都相当于这台服务器上运行控制台的那个系统用户的一个 shell。只给你会给一个 shell 的人开账号。**

要真的隔开，需要每个账号的命令跑在各自的边界里——第 20 章的沙箱接上、而且只读挂载只挂这个账号自己的目录，或者干脆每个账号一个系统用户。这一章没有做，写在 §19 "留给下一个人的" 第一条。

### 13.2 改写时发现：运营者的 key，发给了一个账号自己选的地址

上一节说的是"一个想做坏事的账号能做什么"。这一节是一件**不需要执行任何命令**、甚至可能是无意中发生的事。

第 19 章写的 `resolve_api_key`（第一版）：

```python
    if provider.get("api_key"):
        return str(provider["api_key"])
    if provider.get("provider") == "openai":
        return os.environ.get("OPENAI_API_KEY")
    return None
```

模型来源没填 key、类型是 `openai`，就用服务器环境里的 `OPENAI_API_KEY`——**不管 `base_url` 是什么**。单人的时候，`base_url` 是运营者自己填的。第 23 章之后，模型来源是**每个账号自己的记录**，任何账号都能添加一个。量一下：一个非管理员账号 `mallory`，添加一个 `openai` 来源，`base_url` 指向自己的一个服务器，开一轮对话：

```
$ uv run python envkey23.py
send: 202
mallory's server received: ['Authorization: Bearer sk-operators-own-key']
```

（`envkey23.py`：运营者 bootstrap，建一个普通账号 `mallory`；`mallory` 登录、添加并激活一个来源、建 thread、发一句话；一个本地的 `asyncio.start_server` 冒充"模型"，记下收到的 `Authorization` 头。）

不需要审批、不需要模型配合、不需要任何命令。一个**善意的**账号也会触发它：把 `base_url` 填成一个"兼容 OpenAI 的"第三方服务、key 栏留空——运营者的 OpenAI key 就发给了那个第三方。（这个缺口在第 19 章的单人控制台里也存在，只是那时填 `base_url` 的就是 key 的主人。）

修法：环境里的 key 只发给 OpenAI 自己。

```python
def resolve_api_key(provider: dict[str, Any]) -> str | None:
    """The pasted key, or the environment, never a file.

    A key typed into the browser is kept in `providers.json` because there is
    nowhere else for it to live; a key that came from `OPENAI_API_KEY` is read
    at call time and never written down, so clearing the form in the browser
    cannot copy the environment's key into the file.
    """
    if provider.get("api_key"):
        return str(provider["api_key"])
    # The environment's key goes to OpenAI and nowhere else.  The first
    # version handed it to whatever `base_url` the provider record named, and
    # after chapter 23 a provider record is something *any account* writes:
    # measured when the chapter was rewritten, a non-admin account pointed a
    # provider at its own server and received the operator's
    # `OPENAI_API_KEY` in the first request.  The same rule spares a
    # single-user console from sending its OpenAI key to a third-party
    # "OpenAI-compatible" endpoint because the key field was left blank.
    if provider.get("provider") == "openai" and _is_openai(provider.get("base_url", "")):
        return os.environ.get("OPENAI_API_KEY")
    return None


OPENAI_HOSTS = frozenset({"api.openai.com"})


def _is_openai(base_url: str) -> bool:
    parts = urlsplit(base_url)
    return parts.scheme == "https" and (parts.hostname or "") in OPENAI_HOSTS
```

> - 必须是 `https`，主机名必须**正好**是 `api.openai.com`——`api.openai.com.evil.example` 不算（`hostname` 取的是整个主机名，比较是相等，不是前缀）。
> - 填了自己的 key 的来源不受影响。

```python
def test_the_environment_key_goes_to_openai_and_nowhere_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minicodex.web.runtime import resolve_api_key

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    openai = {"provider": "openai", "base_url": "https://api.openai.com/v1"}
    assert resolve_api_key(openai) == "sk-env"
    for elsewhere in (
        "http://api.openai.com/v1",  # not https
        "https://api.openai.com.evil.example/v1",
        "https://openai-proxy.example/v1",
        "http://127.0.0.1:9/v1",
    ):
        assert resolve_api_key({"provider": "openai", "base_url": elsewhere}) is None, elsewhere
    assert (
        resolve_api_key({"provider": "openai", "base_url": "http://x/v1", "api_key": "own"})
        == "own"
    )


def test_the_operators_key_is_not_sent_to_a_url_another_account_chose(
    console: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured: a non-admin account adds an `openai` provider with no key and
    its own `base_url`; the first version's `resolve_api_key` filled in the
    server's `OPENAI_API_KEY`, and the account's server received it.

    `mallory` is a `TestClient` used as a context manager on purpose: one that
    is not runs each request on a loop of its own that closes afterwards, and
    the turn -- a background task -- dies with it before it sends anything.
    (The first draft of this test did exactly that, and passed against the
    unfixed code.)"""
    import http.server
    import threading

    seen: list[str] = []

    class Capture(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append(self.headers.get("authorization") or "")
            self.send_response(500)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-the-operators-own-key")
    created = console.post("/api/accounts", json={"key": "mallory", "password": SECOND_PASSWORD})
    assert created.status_code == 200, created.text
    try:
        with TestClient(console.app) as mallory:
            signed = mallory.post(
                "/api/auth/login", json={"key": "mallory", "password": SECOND_PASSWORD}
            )
            assert signed.status_code == 200
            provider = mallory.post(
                "/api/providers",
                json={
                    "name": "mine",
                    "provider": "openai",
                    "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    "model": "x",
                },
            ).json()
            mallory.post(f"/api/providers/{provider['id']}/activate")
            thread_id = _thread(mallory, workspace)
            sent = mallory.post(f"/api/threads/{thread_id}/messages", json={"text": "hi"})
            assert sent.status_code == 202
            running = console.app.state.console.running  # type: ignore[attr-defined]
            deadline = time.time() + 30
            while running and time.time() < deadline:
                time.sleep(0.05)
            assert not running, "the turn did not finish"
    finally:
        server.shutdown()
    assert seen, "the turn never reached the provider"
    assert all("sk-the-operators-own-key" not in header for header in seen), seen
```

> - 第一个：几个"看起来像但不是"的地址都拿不到环境里的 key。
> - 第二个：上面那个测量，写成测试——非管理员账号、自己的"模型"服务器（这次用标准库的 `http.server` 在另一个线程里跑）、等这一轮**真的结束**。断言那个服务器收到过请求（这一轮真的走到了模型），而且没有一个请求带着运营者的 key。说明里讲了这个测试的第一版是怎么在没修的代码上也通过的——§18。

§13.1 说了，一个想拿这个 key 的账号可以用别的办法拿到。这个修法关上的是那条**最容易、而且会无意中走进去**的路。

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

`ci.yml` 里那一行：

```yaml
# Chapter 22 added four vitest tests and did not add this line, so
# they passed on a laptop and had never run on a pull request --
# F23-10, found by chapter 23 looking for its own wiring. Before the
# build, for the same reason pytest is: the cheaper signal first.
npm --prefix frontend test
```

```python
def test_F23_10_the_frontend_tests_run_in_ci(repo_root: Path) -> None:
    """Chapter 22 wrote four vitest tests and never invoked them.

    They were correct, they passed on a laptop, and `ci.yml` ran
    `npm run typecheck` and `npm run build` and nothing else -- so in the four
    weeks that chapter existed, not one pull request ever executed them.

    This is `test_F_1_05_every_mutation_script_runs_somewhere` (chapter 12) one
    language over, and it is here for the same reason that one is: the Python
    probes have had a mechanism since chapter 12 and it caught this chapter's
    own missing workflow entry within a minute of the file being written. The
    TypeScript side had the prose and not the mechanism.

    Deliberately checking the *runner* rather than each file: `vitest run`
    discovers `**/*.test.ts` itself, so naming individual files here would
    reintroduce the list that goes stale. What has to be true is that the
    runner is invoked at all, and that there is something for it to find.
    """
    suites = sorted(p.name for p in (repo_root / "frontend" / "src").rglob("*.test.ts"))
    assert suites, "no frontend test files; this check has stopped covering anything"

    ci = (repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "npm --prefix frontend test" in ci, f"nothing in CI runs {suites}"

    package = json.loads((repo_root / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert "vitest" in package["scripts"]["test"]
```

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
    """Every shell command a workflow actually runs, and nothing else.

    Parsed rather than grepped, so comments, step names and the prose around
    them cannot answer a question about what executes. See F23-11.
    """
    parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return "\n".join(
        step["run"]
        for job in parsed["jobs"].values()
        for step in job["steps"]
        if isinstance(step.get("run"), str)
    )


def test_F_1_05_every_mutation_script_runs_somewhere(repo_root: Path) -> None:
    """A mutation script nobody runs is a file, not a check.

    Added in chapter 12, after finding that chapter 11's nineteen mutations
    had never been wired in: the chapter text said they ran in `postmerge.yml`
    "like chapter 9's", the script was correct, the workflow had steps for 9
    and 10 only, and every test in the repository was green about it.

    The general shape has now appeared often enough to be the rule this book
    keeps arriving at: a promise written in prose is not a mechanism. This is
    the same repair as interlude B's layer checker and chapter 3's description
    snapshot -- state the rule where something executes it.

    Chapter 23 found a hole in it and it is the funniest one in the book, so
    the repair is written out below rather than folded in quietly. This
    function used to search the workflow's whole *text*:

        workflow = (...).read_text(encoding="utf-8")
        missing = [name for name in scripts if name not in workflow]

    A YAML comment is part of that text. So a file naming `probe_mutations.py`
    in a comment satisfied the check whether or not anything ran it -- and
    chapter 13's own repair added exactly such a comment, immediately above the
    step, in every step from 13 on. Deleting the step and keeping the comment
    left this test green. Measured, not reasoned: the run line was removed and
    the check still reported nothing missing.

    Which makes this the check that exists to enforce "a promise written in
    prose is not a mechanism", satisfied by prose. It is now asked of the
    commands the workflow actually runs.
    """
    scripts = sorted(p.name for p in repo_root.glob("probe_mutations*.py"))
    commands = ci_commands(repo_root / ".github/workflows/postmerge.yml")
    missing = [name for name in scripts if name not in commands]
    assert not missing, f"mutation scripts nothing runs: {missing}"


def test_F23_11_a_comment_naming_a_script_does_not_count_as_running_it(
    repo_root: Path, tmp_path: Path
) -> None:
    """The regression test for the check above, on a workflow built for it.

    Written against a synthetic file rather than by editing the real one,
    because a test that mutates the repository to prove a point is a test that
    can leave the repository mutated -- `probe_mutations.py`'s own docstring
    says so, having done it.
    """
    workflow = tmp_path / "postmerge.yml"
    workflow.write_text(
        "jobs:\n"
        "  check:\n"
        "    steps:\n"
        "      # probe_mutations.py runs here, honest\n"
        "      - name: Something else\n"
        "        run: uv run python probe_mutations_ch09.py\n",
        encoding="utf-8",
    )
    commands = ci_commands(workflow)
    assert "probe_mutations_ch09.py" in commands
    assert "probe_mutations.py" not in commands, "a comment is not a step"
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


```python
def set_session_cookie(response: Any, token: str, *, secure: bool) -> None:
    """`HttpOnly`, `SameSite=Lax`, and `Secure` only when it can be.

    `HttpOnly` keeps the token away from `document.cookie`, so an injected
    script on this origin cannot read it out and use it elsewhere. `Lax` is
    what makes a cross-site `POST /api/threads/{id}/messages` from an
    attacker's page arrive without a cookie -- half of this console's CSRF
    defence.  Only half: "site" ignores the port, so a page on another
    `localhost` port is same-site and its POST does carry the cookie.  The
    other half is the `Origin` check `AuthMiddleware` makes on every unsafe
    method (added when chapter 23 was rewritten; the first version of this
    docstring called `Lax` the whole defence).

    `Secure` is conditional, and that is a real trade rather than laziness: set
    unconditionally, the cookie is discarded by the browser on plain
    `http://localhost:8000`, which is how this console is normally run, and
    logging in would appear to succeed and then not work. Set from the
    request's own scheme, it is on wherever it means anything.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Any) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
```

> 上面那段正文和这个函数的说明都说"`Lax` 就是这个控制台的 CSRF 防御，够用"。§7.1 说明了它对**同站**的页面不够；这次改写在关卡里补了 `Origin` 检查，并改了这段说明。

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

```python
LEGACY_RECORDS = ("threads.json", "providers.json", "mcp.json", "sessions")


def adopt_legacy_records(data_dir: Path, owner_key: str) -> list[str]:
    """Move a single-tenant console's records under the first account.

    Four chapters of this console wrote `threads.json` and its friends into the
    top of the data directory, because there was one of everybody. Chapter 23
    puts them under `tenants/<key>/`, and doing that without moving the old
    ones means the operator's first sign-in shows an empty console with all
    their sessions still on disk one directory up -- which reads as data loss
    whether or not it is.

    Called exactly once, from the bootstrap route, when the first account is
    created. Not on every start: a console that adopts stray files at the top
    of its data directory whenever it finds them would hand the *next* account
    whatever the previous one left behind.

    A rename, not a copy, and a target that already exists is skipped rather
    than merged into -- there is nothing here that knows how to reconcile two
    `threads.json` files, and pretending otherwise is how one of them is lost
    quietly.  The others are still moved: the first version of this docstring
    said it "stops at the first" existing target, which the loop never did.
    """
    target = data_dir / "tenants" / owner_key
    moved: list[str] = []
    for name in LEGACY_RECORDS:
        source = data_dir / name
        if not source.exists() or (target / name).exists():
            continue
        target.mkdir(parents=True, exist_ok=True)
        source.rename(target / name)
        moved.append(name)
    return moved
```

> 说明的最后一句是这次改写改的：第一版写"遇到已存在的目标就停"，循环其实是**跳过**那一个、继续搬其余的（`continue`，不是 `break`）。上面那段正文（第一版）也是这么说的。

```python
def test_pre_chapter_23_records_are_adopted_by_the_first_account(tmp_path: Path) -> None:
    """Four chapters of consoles wrote these at the top of the data directory.

    Leaving them there means the operator's first sign-in shows an empty
    console with all their sessions still on disk one level up, which reads as
    data loss whether or not it is.
    """
    data = tmp_path / "console"
    (data / "sessions" / "old-thread").mkdir(parents=True)
    (data / "threads.json").write_text("[]", encoding="utf-8")
    (data / "providers.json").write_text("[]", encoding="utf-8")

    moved = adopt_legacy_records(data, "alice")
    assert set(moved) == {"threads.json", "providers.json", "sessions"}
    assert (data / "tenants" / "alice" / "threads.json").exists()
    assert (data / "tenants" / "alice" / "sessions" / "old-thread").is_dir()
    assert not (data / "threads.json").exists()


def test_adoption_never_overwrites_what_the_account_already_has(tmp_path: Path) -> None:
    """There is nothing here that knows how to merge two `threads.json` files."""
    data = tmp_path / "console"
    (data / "tenants" / "alice").mkdir(parents=True)
    (data / "tenants" / "alice" / "threads.json").write_text('["mine"]', encoding="utf-8")
    (data / "threads.json").write_text('["older"]', encoding="utf-8")

    assert adopt_legacy_records(data, "alice") == []
    assert (data / "tenants" / "alice" / "threads.json").read_text(encoding="utf-8") == '["mine"]'
    assert (data / "threads.json").exists(), "the older file is left where it was, not deleted"
```

**收养的只是数据目录里的东西。** 第一版没有说的另一半：单人控制台的记忆在 `~/.minicodex/memories`、远程 MCP 的 OAuth token 在 `~/.minicodex/mcp_tokens.json`（第 16、21 章，`DEFAULT_OWNER` 的位置）；第 23 章之后，控制台读的是 `~/.minicodex/tenants/<key>/memories` 和 `.../mcp_tokens.json`。这两样没有被收养——升级以后，控制台里的记忆是空的，配过 OAuth 的远程服务器显示"需要连接"。不搬是有理由的：命令行（`minicodex ask`）还在用原来的位置，搬走会让它丢东西；复制又会让同一个 refresh token 存在两处，一边刷新就让另一边作废（第 21 章 F21-11 那个"用过的 refresh token"）。这一章不替运营者做这个决定，写在这里。

其余几个不算故障的测试：

```python
def test_a_stored_hash_carries_the_parameters_it_was_written_with() -> None:
    """Which is what makes raising the cost later a one-line change."""
    encoded = hash_password("a-long-enough-password", n=2**4)
    kind, n, r, p, _salt, _key = encoded.split("$")
    assert (kind, n, r, p) == ("scrypt", "16", "8", "1")
    assert verify_password("a-long-enough-password", encoded)
    assert not verify_password("a-long-enough-passwerd", encoded)


def test_a_malformed_hash_is_a_failed_login_not_a_crash() -> None:
    assert verify_password("anything", "not-a-hash-at-all") is False
    assert verify_password("anything", "scrypt$$$$$") is False


def test_a_short_password_is_refused(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "accounts.json")
    with pytest.raises(AccountError) as raised:
        store.create("alice", "x" * (MIN_PASSWORD_LENGTH - 1))
    assert str(MIN_PASSWORD_LENGTH) in str(raised.value)


def test_an_account_key_that_is_not_a_safe_directory_name_is_refused(tmp_path: Path) -> None:
    """The key becomes a path segment under two different trees."""
    store = AccountStore(tmp_path / "accounts.json")
    for bad in ("../escape", "has space", "", "tenants", "x" * 65):
        with pytest.raises(AccountError):
            store.create(bad, "a-long-enough-password")


def test_an_account_key_is_case_folded_rather_than_refused(tmp_path: Path) -> None:
    """Chapter 20 said what to do here, and this is doing it.

    `tenancy._SAFE_KEY` refuses upper case and explains why: two owners are
    separated by the spelling of one path segment, and on NTFS or a default
    macOS volume `Alice` and `alice` are one directory. The comment goes on to
    say that the fix, if the pattern is ever widened, is "to case-fold the key
    before it ever becomes a path, not to trust the filesystem to keep them
    apart". So `create` folds, and the second registration collides on the
    folded name instead of silently sharing a directory with the first.
    """
    store = AccountStore(tmp_path / "accounts.json")
    assert store.create("Alice", "a-long-enough-password").key == "alice"
    with pytest.raises(AccountError) as raised:
        store.create("ALICE", "a-long-enough-password")
    assert "already exists" in str(raised.value)


def test_the_session_cookie_is_httponly_and_samesite(anon: TestClient) -> None:
    sign_in(anon)
    header = anon.post(
        "/api/auth/login", json={"key": CONSOLE_ACCOUNT, "password": CONSOLE_PASSWORD}
    ).headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    # Not `Secure`, because this request was plain http and a `Secure` cookie
    # on `http://localhost:8000` is dropped by the browser -- a login that
    # appears to succeed and then does not work.
    assert "Secure" not in header


def test_a_session_is_stored_hashed_so_the_table_cannot_leak_one() -> None:
    table = SessionTable()
    token, session = table.create("alice")
    assert token not in repr(table)
    assert session.token_hash != token
    assert table.touch(token) is session


def test_an_expired_session_is_the_same_answer_as_a_forged_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = SessionTable()
    token, session = table.create("alice")
    session.last_seen -= 60 * 60 * 24
    assert table.touch(token) is None
    assert table.touch("never-issued") is None
    assert len(table) == 0, "the expired entry is dropped where it is noticed"
```

### 15.1 前端那一侧

第一版这一章没有讲前端。登录页：

```tsx
import { useState } from "react";
import { ApiError, api } from "../api";
import type { AuthStatus } from "../types";

/**
 * The first screen, and for a fresh console the only one there is.
 *
 * Two forms, chosen by `has_account` -- which is the whole reason
 * `GET /api/auth/status` is public. A console nobody has claimed yet shows the
 * bootstrap form and asks for the token the server printed on startup; a
 * console with an account shows the login form.
 *
 * The bootstrap form says where the token comes from, because "paste the
 * one-time token" is not a helpful instruction to somebody who did not read
 * the terminal they started the server in.
 */
export function SignIn({ status, onSignedIn }: { status: AuthStatus; onSignedIn: () => void }) {
  const [key, setKey] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const first = !status.has_account;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (first) await api.bootstrap(token.trim(), key.trim(), password);
      else await api.login(key.trim(), password);
      onSignedIn();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
      setPassword("");
    }
  }

  return (
    <div className="signin-screen">
      <form className="signin-card" onSubmit={(e) => void submit(e)}>
        <h1 className="signin-title">minicodex</h1>
        <p className="signin-lede">
          {first
            ? "Nobody has claimed this console yet. The terminal running it printed a one-time token; paste it here to create the first account."
            : "Sign in to the console."}
        </p>

        {first && (
          <label className="form-row">
            <span className="settings-label">One-time token</span>
            <input
              className="settings-input"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder="printed by minicodex serve"
            />
          </label>
        )}

        <label className="form-row">
          <span className="settings-label">Account name</span>
          <input
            className="settings-input"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            autoComplete="username"
            spellCheck={false}
            placeholder="lower case, digits, - and _"
          />
        </label>

        <label className="form-row">
          <span className="settings-label">Password</span>
          <input
            className="settings-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={first ? "new-password" : "current-password"}
          />
        </label>

        {first && (
          <p className="settings-help">
            At least 10 characters. Length only &mdash; no character classes, which push people
            towards <code>Passw0rd!</code> and are worth less than four more characters.
          </p>
        )}

        {error && <p className="form-error">{error}</p>}

        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? "Working…" : first ? "Create this account" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
```

> - 先问 `/api/auth/status`：还没有账号就显示"用 bootstrap 令牌建第一个账号"的表单，否则显示登录表单。
> - 登录成功后调 `onSignedIn`，`App.tsx` 重新加载。

账号页（自己的会话、改口令；管理员还能建账号、停用账号）：

```tsx
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { Account, AuthStatus, BrowserSession } from "../types";

function when(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString();
}

/**
 * Everything about the person using this console, on one tab.
 *
 * Three things, and each of them exists because the alternative is a thing
 * only the operator can do:
 *
 * - **the signed-in devices**, so a session that was opened somewhere you no
 *   longer trust can be ended from here rather than by restarting the server;
 * - **the password**, with the note that changing it signs every other browser
 *   out, because that is what a password change is for;
 * - **the accounts**, for whoever bootstrapped the console.
 *
 * The quota is shown rather than hidden until it bites. A limit a person only
 * discovers as a 429 is a limit they experience as a bug.
 */
export function AccountPanel({ status, onSignedOut }: { status: AuthStatus; onSignedOut: () => void }) {
  const [sessions, setSessions] = useState<BrowserSession[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [error, setError] = useState("");
  const account = status.account;

  const reload = useCallback(async () => {
    setSessions(await api.mySessions());
    if (account?.admin) setAccounts(await api.accounts());
  }, [account?.admin]);

  useEffect(() => {
    void reload().catch((exc) => setError(String(exc)));
  }, [reload]);

  if (!account) return null;

  async function guard(work: () => Promise<void>) {
    setError("");
    try {
      await work();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  }

  return (
    <div className="ext-panel active">
      <div className="ext-toolbar">
        <p className="ic-desc" style={{ maxWidth: "46ch" }}>
          Signed in as <strong>{account.key}</strong>
          {account.admin && " (administrator)"}. Threads, providers, MCP servers, memories and
          OAuth tokens all belong to this account and are not visible to any other.
        </p>
        <button className="btn btn-sm" onClick={() => void guard(async () => {
          await api.logout();
          onSignedOut();
        })}>
          Sign out
        </button>
      </div>

      {error && <p className="form-error">{error}</p>}

      {status.quota && (
        <div className="card-list">
          <div className="item-card">
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">Usage</span>
              </div>
              <div className="ic-meta">
                {status.quota.running} of {status.quota.concurrent_limit} turns running &middot;{" "}
                {status.quota.used_this_hour} of {status.quota.hourly_limit} started this hour
              </div>
              <p className="settings-help">
                Charged when a turn is claimed, not when it finishes: a turn that fails costs the
                same model call as one that succeeds.
              </p>
            </div>
          </div>
        </div>
      )}

      <h3 className="settings-label" style={{ marginTop: 18 }}>
        Signed-in browsers
      </h3>
      <div className="card-list">
        {sessions.map((s) => (
          <div className="item-card" key={s.id}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{s.user_agent || "unknown browser"}</span>
                {s.current && <span className="ic-active-tag">This one</span>}
              </div>
              <div className="ic-meta">
                started {when(s.created)} &middot; last seen {when(s.last_seen)}
              </div>
            </div>
            <div className="ic-actions">
              {!s.current && (
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() =>
                    void guard(async () => {
                      await api.revokeSession(s.id);
                      await reload();
                    })
                  }
                >
                  Sign out
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
      <p className="settings-help">
        Sessions live in the server process, so restarting it signs everybody out. Signing out
        everywhere also cancels any turn this account left running &mdash; revoking access that
        leaves work running has not revoked anything.
      </p>
      <button
        className="btn btn-ghost btn-sm"
        onClick={() =>
          void guard(async () => {
            await api.revokeAllSessions();
            onSignedOut();
          })
        }
      >
        Sign out everywhere
      </button>

      <ChangePassword onChanged={() => void reload()} />

      {account.admin && <Accounts accounts={accounts} onChanged={() => void reload()} />}
    </div>
  );
}

function ChangePassword({ onChanged }: { onChanged: () => void }) {
  const [current, setCurrent] = useState("");
  const [replacement, setReplacement] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  return (
    <div className="add-form open" style={{ marginTop: 18 }}>
      <h3 className="settings-label">Change password</h3>
      <div className="form-grid">
        <div className="form-row">
          <span className="settings-label">Current</span>
          <input
            className="settings-input"
            type="password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            autoComplete="current-password"
          />
        </div>
        <div className="form-row">
          <span className="settings-label">New</span>
          <input
            className="settings-input"
            type="password"
            value={replacement}
            onChange={(e) => setReplacement(e.target.value)}
            autoComplete="new-password"
          />
        </div>
      </div>
      <p className="settings-help">
        Every other signed-in browser is signed out. This one stays, on a new session.
      </p>
      <div className="form-actions">
        <span className="form-error">{error}</span>
        {note && <span className="ic-meta">{note}</span>}
        <button
          className="btn btn-primary btn-sm"
          onClick={() =>
            void (async () => {
              setError("");
              setNote("");
              try {
                await api.changePassword(current, replacement);
                setCurrent("");
                setReplacement("");
                setNote("changed; other browsers signed out");
                onChanged();
              } catch (exc) {
                setError(exc instanceof ApiError ? exc.message : String(exc));
              }
            })()
          }
        >
          Change it
        </button>
      </div>
    </div>
  );
}

function Accounts({ accounts, onChanged }: { accounts: Account[]; onChanged: () => void }) {
  const [key, setKey] = useState("");
  const [password, setPassword] = useState("");
  const [roots, setRoots] = useState("");
  const [error, setError] = useState("");

  return (
    <div style={{ marginTop: 22 }}>
      <h3 className="settings-label">Accounts</h3>
      <div className="card-list">
        {accounts.map((a) => (
          <div className="item-card" key={a.key}>
            <div className="ic-main">
              <div className="ic-title-row">
                <span className="ic-name">{a.key}</span>
                {a.admin && <span className="ic-active-tag">admin</span>}
                {a.disabled && <span className="status-chip status-bad">disabled</span>}
              </div>
              <div className="ic-meta">
                {a.workspace_roots.length
                  ? `may open: ${a.workspace_roots.join(", ")}`
                  : "may open any directory on this server"}
              </div>
            </div>
            <div className="ic-actions">
              {!a.disabled && (
                <button
                  className="btn btn-ghost btn-sm"
                  onClick={() =>
                    void (async () => {
                      setError("");
                      try {
                        await api.disableAccount(a.key);
                        onChanged();
                      } catch (exc) {
                        setError(exc instanceof ApiError ? exc.message : String(exc));
                      }
                    })()
                  }
                >
                  Disable
                </button>
              )}
            </div>
          </div>
        ))}
      </div>

      <div className="add-form open">
        <div className="form-grid">
          <div className="form-row">
            <span className="settings-label">Account name</span>
            <input
              className="settings-input"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              spellCheck={false}
            />
          </div>
          <div className="form-row">
            <span className="settings-label">Password</span>
            <input
              className="settings-input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </div>
        </div>
        <div className="form-row">
          <span className="settings-label">Directories this account may open</span>
          <input
            className="settings-input"
            value={roots}
            onChange={(e) => setRoots(e.target.value)}
            spellCheck={false}
            placeholder="/srv/work/bob    (comma separated; empty means anywhere)"
          />
        </div>
        <p className="settings-help">
          Leave it empty only for an account you would give a shell to. A workspace is any absolute
          path on this server, and the agent&rsquo;s whole job is to read files and run commands
          there &mdash; so this, not the tenancy underneath it, is what keeps two accounts out of
          each other&rsquo;s directories.
        </p>
        <div className="form-actions">
          <span className="form-error">{error}</span>
          <button
            className="btn btn-primary btn-sm"
            onClick={() =>
              void (async () => {
                setError("");
                try {
                  await api.addAccount({
                    key: key.trim(),
                    password,
                    admin: false,
                    workspace_roots: roots
                      .split(",")
                      .map((r) => r.trim())
                      .filter(Boolean),
                  });
                  setKey("");
                  setPassword("");
                  setRoots("");
                  onChanged();
                } catch (exc) {
                  setError(exc instanceof ApiError ? exc.message : String(exc));
                }
              })()
            }
          >
            Create account
          </button>
        </div>
      </div>
    </div>
  );
}
```

`api.ts` 里的一个关键改动——遇到 401 就回到登录页：

```typescript
 * What to do when the server says nobody is signed in.
 *
 * A session ends on its own -- twelve hours from sign-in, two hours idle
 * (`sessions.py`) -- so this is not an edge case, it is what happens to
 * anybody who leaves a tab open over lunch. Without this hook the console
 * shows a page full of stale data and every button reports an error; with it,
 * the app drops back to the sign-in screen at the first 401.
 *
 * A module-level callback rather than a React context because `call` is not a
 * component and never will be, and threading a setter through every one of
 * these thirty methods to reach one `if` is not an abstraction, it is a tax.
 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

/** The three routes that answer without a session; a 401 from one of them is
 *  a wrong password, not an expired session, and must not bounce the app. */
const PUBLIC_PATHS = new Set(["/api/auth/status", "/api/auth/login", "/api/auth/bootstrap"]);
```

> - **`setUnauthorizedHandler`**：`App.tsx` 注册一个"回到登录页"的函数。
> - **`PUBLIC_PATHS`**：这三个路径的 401 是"口令不对"，不是"会话过期"，不能把人弹回登录页。`call` 里多了一行：`if (res.status === 401 && !PUBLIC_PATHS.has(path)) onUnauthorized?.();`。

新加的那一批调用（登录、登出、会话、改口令、账号管理）：

```typescript
authStatus: () => call<AuthStatus>("GET", "/api/auth/status"),
bootstrap: (token: string, key: string, password: string) =>
  call<{ account: Account }>("POST", "/api/auth/bootstrap", { token, key, password }),
login: (key: string, password: string) =>
  call<{ account: Account }>("POST", "/api/auth/login", { key, password }),
logout: () => call<{ ok: boolean }>("POST", "/api/auth/logout"),
mySessions: () => call<BrowserSession[]>("GET", "/api/auth/sessions"),
revokeSession: (id: string) => call<{ ok: boolean }>("DELETE", `/api/auth/sessions/${id}`),
revokeAllSessions: () =>
  call<{ sessions: number; turns: number }>("POST", "/api/auth/sessions/revoke-all"),
changePassword: (current: string, replacement: string) =>
  call<{ account: Account }>("POST", "/api/auth/password", { current, replacement }),

accounts: () => call<Account[]>("GET", "/api/accounts"),
addAccount: (body: {
  key: string;
  password: string;
  admin: boolean;
  workspace_roots: string[];
}) => call<Account>("POST", "/api/accounts", body),
disableAccount: (key: string) => call<Account>("POST", `/api/accounts/${key}/disable`),
```

前端测试：

```typescript
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, setUnauthorizedHandler } from "./api";

/**
 * The one piece of chapter 23's frontend that is logic rather than layout.
 *
 * A session expires on its own -- twelve hours from sign-in, two hours idle --
 * so an open tab becomes a signed-out tab without anybody clicking anything.
 * What the console does at that moment is a decision, and the wrong version of
 * it (show the error, keep the stale page) is what most consoles do.
 *
 * `fetch` is stubbed rather than mocked at the module boundary, because the
 * behaviour under test is exactly "what does `call` do with a 401" and
 * replacing `call` would leave nothing.
 */

function respond(status: number, body: unknown = {}) {
  return vi.fn().mockResolvedValue({
    ok: status < 400,
    status,
    statusText: String(status),
    json: async () => body,
  } as unknown as Response);
}

afterEach(() => {
  setUnauthorizedHandler(null);
  vi.unstubAllGlobals();
});

describe("a 401 on a guarded route", () => {
  it("tells the app it has been signed out", async () => {
    vi.stubGlobal("fetch", respond(401, { detail: "not signed in" }));
    const signedOut = vi.fn();
    setUnauthorizedHandler(signedOut);

    await expect(api.workspaces()).rejects.toBeInstanceOf(ApiError);
    expect(signedOut).toHaveBeenCalledOnce();
  });

  it("still reports the error, so a caller that wants it gets it", async () => {
    vi.stubGlobal("fetch", respond(401, { detail: "not signed in" }));
    setUnauthorizedHandler(vi.fn());
    await expect(api.workspaces()).rejects.toMatchObject({ status: 401 });
  });
});

describe("a 401 from the sign-in routes themselves", () => {
  it("is a wrong password, not an expired session", async () => {
    // The distinction matters: bouncing the app back to the sign-in screen on
    // a failed sign-in would clear the form the person is typing into and lose
    // the message telling them why it failed.
    vi.stubGlobal("fetch", respond(401, { detail: "that account name and password do not match" }));
    const signedOut = vi.fn();
    setUnauthorizedHandler(signedOut);

    await expect(api.login("alice", "wrong")).rejects.toMatchObject({ status: 401 });
    expect(signedOut).not.toHaveBeenCalled();
  });
});

describe("everything else", () => {
  it("does not fire the handler", async () => {
    vi.stubGlobal("fetch", respond(404, { detail: "no thread 'nope'" }));
    const signedOut = vi.fn();
    setUnauthorizedHandler(signedOut);

    await expect(api.thread("nope")).rejects.toMatchObject({ status: 404 });
    expect(signedOut).not.toHaveBeenCalled();
  });

  it("passes a successful response through untouched", async () => {
    vi.stubGlobal("fetch", respond(200, { has_account: true, signed_in: false, account: null }));
    setUnauthorizedHandler(vi.fn());
    await expect(api.authStatus()).resolves.toMatchObject({ has_account: true });
  });
});
```

> 五个测试：一个受保护路由的 401 会调用那个处理函数，错误仍然抛出；公开路由的 401（口令不对）**不**调用它；成功的响应原样通过。

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

```python
def test_the_password_comparison_is_constant_time(repo_root: Path) -> None:
    """Asserted on the source, and that is the honest place for it.

    `hmac.compare_digest(a, b)` and `a == b` return the same value for every
    input. No behavioural test can tell them apart, and a timing test over a
    32-byte comparison inside CPython measures the interpreter, not the
    comparison -- the mutation probe found exactly this and it is the one
    survivor in chapter 23 that stays a survivor.

    The property is real and is a property of the *text*: this line must use
    the constant-time primitive. So the test reads the line. Chapter 19 already
    does this for a frontend confirmation dialog (F19-11); the shape is the
    same, and its limits are worth naming -- a static assertion cannot tell
    whether the function does what its name says, only that it was called.
    """
    source = (repo_root / "src" / "minicodex" / "web" / "accounts.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest(computed, expected)" in source
    assert "computed == expected" not in source
```

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

全书 **259** 条（这张表是本章的 11 条；全书总数以 `FAULTS.md` 为准）。

改写时找到的，不占编号（`FAULTS.md` 里一张单独的表）：

| 问题 | 怎么发现的 |
|---|---|
| 任何账号都能让控制台把运营者的 `OPENAI_API_KEY` 发到它选的地址（§13.2） | 🟢 用一个非管理员账号去试 |
| 每个账号都能以服务器用户的身份执行命令，"多租户"不是安全边界，第一版没说（§13.1） | 🟣 读代码，再量 `/proc` |
| 同站页面的无正文 POST 带着 cookie 进来（§7.1） | 🟢 |
| `accounts.json`、`providers.json` 是 0644（§3.2） | 🟢 |
| `minicodex-live` 的控制台一轮对话都跑不起来（§18） | 🟡 一个等到模型被调用的测试 |
| 不用 `with` 的 `TestClient` 让后台回合悄悄消失（§18） | 🟡 自己的测试在没修的代码上是绿的 |

---

## 18. 验证

```bash
uv run ruff format --check . && uv run ruff check .
uv run python scripts/check_layers.py --quiet
uv run pytest -q
uv run python probe_mutations_ch23.py
cd frontend && npm ci && npx tsc --noEmit && npx vitest run && npx vite build
```

这次改写在一台 Linux 机器上（Python 3.13）的结果，第 23 步和 `minicodex-live`：

```
steps/step23_multi_user  ruff + layers ok   2069 passed, 140 warnings
steps/step23_multi_user  frontend: tsc ok, vitest 10 passed (10), build ok
minicodex-live           ruff + layers ok   1 failed, 2132 passed, 13 skipped, 184 warnings
    FAILED tests/test_live_review.py::test_M5_a_forged_exit_line_does_not_fool_the_audit[asyncio]
minicodex-live           frontend: tsc ok, vitest 10 passed (10), build ok
steps/step23_multi_user  probe_mutations_ch19.py   every mutation was caught.
steps/step23_multi_user  probe_mutations_ch22.py   every mutation was caught.
steps/step23_multi_user  probe_mutations_ch23.py   every mutation was caught.
```

`minicodex-live` 那一个失败和这一章无关（第 21 章 §15）。第一版这一节的数字（"1927 passed, 12 skipped"、那份 skip 清单）是在 Windows 上数的，不适用于这里。

变异检查，第一版 28 条，这次改写加了 4 条：

```python
# ---- added when chapter 23 was rewritten --------------------------------
(
    "src/minicodex/web/runtime.py",
    "the operator's OPENAI_API_KEY goes to whatever base_url a provider names",
    '    if provider.get("provider") == "openai" and _is_openai(provider.get("base_url", "")):',
    '    if provider.get("provider") == "openai":',
),
(
    "src/minicodex/web/auth.py",
    "a cross-origin POST is let through as long as it carries the cookie",
    '        if scope["type"] == "http" and scope.get("method") not in _SAFE_METHODS:',
```

```
$ uv run python probe_mutations_ch23.py
32 mutations, tests/test_faults_ch23.py tests/test_packaging.py

    7 test(s) fail  <-  the gate lets an unauthenticated request through instead of refusing
   29 test(s) fail  <-  every path becomes public, not just the three on the list
    1 test(s) fail  <-  a session whose account was disabled keeps working
    2 test(s) fail  <-  the gate skips websockets -- BaseHTTPMiddleware's behaviour, restored
    1 test(s) fail  <-  a cross-origin websocket handshake is accepted
    3 test(s) fail  <-  an Origin from another host counts as same-origin
    4 test(s) fail  <-  every account shares one record store again
    1 test(s) fail  <-  every account shares one approval broker again
    1 test(s) fail  <-  a session is revoked by id alone, without checking whose it is
    1 test(s) fail  <-  the websocket subscribes without checking the thread belongs to the caller
    2 test(s) fail  <-  the memory panel goes back to the workspace directory nothing reads
    1 test(s) fail  <-  a turn loads whatever memory the default owner has, not this owner's
    1 test(s) fail  <-  an unknown account answers without hashing anything -- the timing leak
    1 test(s) fail  <-  the password comparison stops at the first differing byte
    1 test(s) fail  <-  the bootstrap token still works after it has been redeemed
    1 test(s) fail  <-  any string is accepted as the bootstrap token
    1 test(s) fail  <-  an unreadable accounts.json is treated as an empty one
    1 test(s) fail  <-  a session never expires
   32 test(s) fail  <-  the raw token is kept in the table instead of its hash
    1 test(s) fail  <-  signing out everywhere leaves this account's turns running
    1 test(s) fail  <-  changing a password leaves every other session signed in
    2 test(s) fail  <-  the hourly window is never enforced
    2 test(s) fail  <-  the concurrency limit is never enforced
    2 test(s) fail  <-  a claim is not recorded, so the hour never fills up
    1 test(s) fail  <-  a turn refused for want of a provider still spends the hour
    1 test(s) fail  <-  the CI wiring check goes back to searching the workflow's whole text
    2 test(s) fail  <-  an account's workspace roots are not enforced
    1 test(s) fail  <-  'inside a root' becomes a string prefix again
    2 test(s) fail  <-  the operator's OPENAI_API_KEY goes to whatever base_url a provider names
    1 test(s) fail  <-  a cross-origin POST is let through as long as it carries the cookie
    1 test(s) fail  <-  a tenant's records (API keys included) are written readable by everyone
    1 test(s) fail  <-  accounts.json is written readable by everyone

every mutation was caught.
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

### 18.1 改写时发现：一个在没修的代码上也是绿的测试

§13.2 那个"运营者的 key 没有发出去"的测试，第一次写的时候是这样的：用 `_second_account` 拿到 `mallory` 的客户端，让它开一轮，然后断言"捕获到的请求里没有那个 key"。

修好代码，测试绿了。把 `runtime.py` 换回第一版——**还是绿的**。捕获到的请求是空的：这一轮根本没有走到模型。

原因在测试客户端上。`_second_account` 返回的是 `TestClient(console.app)`，**没有**用 `with` 进入。这样的客户端每个请求都在一个**临时的事件循环**上处理，请求一结束，那个循环就关掉了——一轮对话是一个后台任务（`console.running[key] = create_task(...)`），它跟着那个循环一起消失，一个字节都没发出去。断言"没有一个请求带着 key"，对一个空列表永远成立。

修法有两半：测试里 `mallory` 用 `with TestClient(console.app) as mallory:`；**再加一句 `assert seen`**——"这一轮真的走到了模型"——让一个什么都没发生的测试不可能通过。这和 §5 的 `checked > 20` 是同一个道理：一条检查里最便宜的一行，是"我到底检查了几个东西"。`_second_account` 的说明里加了一段警告。

### 18.2 改写时发现：`minicodex-live` 的控制台一轮也跑不起来

同一个测试搬到 `minicodex-live` 里，修好之后还是失败：`the turn never reached the provider`。看那一轮的事件：

```
{"type": "turn_started", "text": "hi"}
{"type": "error", "text": "FrozenInstanceError: cannot assign to field 'shell'"}
{"type": "turn_finished"}
```

`minicodex-live` 的 `run_turn` 给 shell 包了一层审计：

```python
    if audit is not None:
        context.shell = audited(context.shell)
```

`ToolContext` 是 `@dataclass(frozen=True)`——不能给字段赋值。这一行在调用模型**之前**抛异常，而控制台的回合总是带着审计。也就是说，**交付版的控制台，从它被放进这个仓库的那一刻起，没有一轮对话能跑到模型**。`git stash` 掉这次的所有改动、在 `main` 上重跑，同样的错误。

`minicodex-live` 有两千多个测试，没有一个让控制台的一轮对话走到模型那一步。修法是一行：`context = replace(context, shell=audited(context.shell))`——造一个换了 `shell` 的新对象。§13.2 的那个测试就是它的回归测试。

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

`tenancy.py` 的**代码**一行没改（只在说明里加了上面这一段）。那是提前三章写下那道缝的全部意义。

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

- **每个账号都是这台服务器上的一个 shell。** 这一条第一版没有写，而它应该排第一（§13.1）。在每个账号的命令都跑在只属于它自己的边界里之前，只给你会给一个 shell 的人开账号。


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
