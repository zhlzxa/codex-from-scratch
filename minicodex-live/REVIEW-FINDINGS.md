# minicodex-live 修复清单

三份工作汇总：全量代码评审（约 24k 行源码 + 19k 行测试 + 部署栈逐行读完）、与
`../codex-main`（上游 codex）逐 feature 机制对照、以及 Recorder / RolloutWriter /
History 三者关系的辨析结论。

每条带编号、严重度、位置锚点（文件与行号以当前 main 为准，已逐一核对）、
问题描述、建议修法。编号规则沿用仓库惯例：

- `H*` 高严重度（安全/正确性承诺被撤销，优先修）
- `M*` 中严重度（可靠性/资源/正确性边角）
- `L*` 低严重度（一致性/卫生，顺手清理）
- `D*` 文档债（文档承诺与代码不符，或对照声明缺失）
- `X*` 与 codex 的机制差异，分"有意偏离（记录即可）"与"值得对齐"两类

按本仓库自己的规矩修：**先复现，后修复，测试钉住**；测试命名沿用
`test_<编号>_<what_it_asserts>`（放 `tests/test_live*.py` 或新建
`tests/test_live_review.py`）。

---

## 一、高严重度（H）

### H1 — 子代理丢沙箱、丢审计：`spawn_agent` 静默撤销 LIVE-01 / LIVE-05

- **位置**：`src/minicodex/subagent.py:349-350`（`child_tools`）；相关：
  `src/minicodex/composition.py:89-105`（`child_tools_builder`）、
  `src/minicodex/web/runtime.py:277,293`（`_AuditedShell`）
- **问题**：子代理的 shell 只复制了 `timeout` 和 `cwd`：

  ```python
  shell = ShellSession(timeout=ctx.parent_shell.timeout)
  shell.cwd = ctx.parent_shell.cwd      # 没有 shell.sandbox = ctx.parent_shell.sandbox
  ```

  子代理的 shell 是 `sandbox=None`，命令裸跑宿主机。而 `runtime.py:277` 的注释
  声称 "sub-agent seeding reads all three (cwd/timeout/sandbox)"——文档相信的事
  代码没做。后果：控制台里一个 `workspace-write`（bwrap 隔离）的线程，模型只要调
  一次 `spawn_agent`，子代理的所有 shell 命令回到 LIVE-01 修复前。
  **连带**：子代理的 shell 也不是 `_AuditedShell`，其命令完全绕过审计日志
  （LIVE-05 的"每条命令含退出码"只对父代理成立）。
- **对照**：codex 的 `apply_spawn_agent_runtime_overrides`
  （`codex-rs/core/src/tools/handlers/multi_agents_common.rs:258-280`）把
  approval policy / sandbox（permission profile）/ cwd 三样**显式**写进子配置，
  注释专门说"忘了会让子代理和父代理 disagree"。上游的处理方式反衬出此 bug。
- **修法**：
  1. `child_tools` 里补 `shell.sandbox = ctx.parent_shell.sandbox`；
  2. 审计：把 audit/actor 从 `_AuditedShell` 的属性改成随 shell 传递的通道
     （或让 `child_tools_builder` 接受一个 shell 包装函数），保证子代理命令同样
     进 `audit.jsonl`；
  3. 测试：构造带 FakeSandbox 的父 shell，spawn 子代理，断言子代理 shell 的
     sandbox 与父一致；断言子代理命令出现在 audit 记录里。
- **验证方式**：`probe_sandbox.py kill` 语义不变；`tests/test_live.py` 的
  LIVE-01 组补子代理用例。

### H2 — `confined=True` 从未被生产代码传入：chapter 20 的收益没有接线

- **位置**：`src/minicodex/approval.py:150-151`（`gate_command`）；
  表定义在 `src/minicodex/policy.py:318-322`
  （`_CONFINED_ALLOWED_BY_MODE`）、docstring 自称
  "chapter 20's whole return on investment"
- **问题**：`judge_command` 有 `confined: bool = False` 参数，沙箱在场时
  `workspace-write` 应放行 WRITE+INTERPRETER（内核已强制边界，审批沦为仪式、
  F05-06 的审批疲劳由此消除）。但全项目 grep 确认：**没有任何生产调用方传
  `confined=True`**，只有测试直接传。结果：即使 bwrap 真的在跑，每条写文件的
  shell 命令仍然弹审批。表和它周围约 40 行注释是死接线。
  方向是 fail-safe 的（只是多问），所以不是漏洞，但它是本项目最典型的病：
  代码、注释、测试三方面都相信一个机制在生产中生效。
- **修法**：
  1. `Session`（`approval.py`）增加对 shell 的可达（或 `gate_command` 增加
     `confined` 参数，由 `run_shell` handler 从 `ctx.shell.sandbox is not None
     and sandbox.unavailable() is None` 计算后传入）；
  2. 同步 `permissions_block`（`approval.py:308`）里 `workspace-write` 的措辞
     ——confined 后"A *shell command* that changes anything still needs
     approval"这句就不再为真，需按 confined 与否分支；
  3. 测试：confined 会话下 `rm -rf build` 在 `workspace-write` 得到
     ALLOW（现测试 `test_faults_ch20.py:275-281` 已有原型，补一条走
     `gate_command` 全链路的）。
- **注意**：与 H1 联动——子代理沙箱修好后，confined 判定在子代理路径同样成立。

### H3 — `AccountStore._read` 违背自己立的不变量：合法 JSON 但非列表 → 静默回退空账号表

- **位置**：`src/minicodex/web/accounts.py:287`
- **问题**：解析失败（JSONDecodeError）会抛 `AccountError`（"refusing to
  treat it as empty"，测试
  `test_an_unreadable_accounts_file_is_not_an_empty_one` 盖住了这条）。但紧接
  的 `return list(data) if isinstance(data, list) else []`：文件能解析、内容是
  `{}` 或被截成的对象时，**静默变成"没有账号"** → `empty()` 为真 → 下次启动
  重发 bootstrap 令牌。这正是上面那个防御想挡住的场景，只是换了一种损坏方式；
  测试恰好没盖住这个分支。
- **修法**：`_read` 里 `data` 非 list 时同样 `raise AccountError(...)`；
  补测试：`accounts.json` 内容为 `"{}"` 时 `all()` 必须抛。

### H4 — `LoginThrottle._pairs` 无限增长：未认证流量可驱动的慢性内存泄漏

- **位置**：`src/minicodex/web/throttle.py:87-101`（`record_failure`）、
  `:103-110`（`record_success` 只清成功者）
- **问题**：`record_failure` 只清理单个 pair 内的过期时间戳，pair 条目本身
  （锁过期、窗口清空后）永不删除。公网部署上攻击者用大量来源 IP 打失败登录，
  每个 IP 留一个字典条目，进程内存随未认证流量无限增长。`record_success`
  路径对纯攻击不存在，无对冲。
- **修法**：在 `check`/`record_failure` 的入口加廉价 sweep：窗口过期**且**
  `locked_until < now` 的 pair 删除（与 `quota._window`、
  `PendingOAuthTable._sweep` 的懒清扫同形）。测试：填 N 个 pair、时间推进过
  窗口+锁，断言 `len(throttle)` 回落。

---

## 二、中严重度（M）

### M1 — CLI 的 `--sandbox-mode` 从不构造沙箱

- **位置**：`src/minicodex/__main__.py` 的 `_ask`（约 166-500 行，全文无
  `for_mode` 调用）；LIVE-02 的启动闸门只护 `serve`（`web/app.py:254`）
- **问题**：`ask` 路径上 `--sandbox-mode workspace-write` 只影响审批提问，
  与 LIVE-01 修复前的控制台一模一样。单用户本地风险低，但旗标在撒谎。
- **修法**：`_ask` 按 mode 构造 `sandbox_for_mode(...)` 并传入
  `tool_context`/`local_tools`（composition 已支持 `sandbox=` 形参）；启动时
  调 `unavailable()`，缺失即拒绝或显式 `--allow-unsandboxed`（对齐 serve 的
  闸门语义）。

### M2 — 后台记忆管线在事件循环上做阻塞 I/O

- **位置**：`src/minicodex/memory_write.py:1180-1187`（`_git`，同步
  `subprocess.run`，timeout 30s），调用链
  `run_pipeline`（`:1315`，async）→ `ensure_repo`（`:1404`）/ `hand_edits` /
  `commit`；以及 `memory_jobs.pending_sessions`（`memory_jobs.py:262-314`）
  在同一循环里同步解析 sessions 目录下每个 rollout
- **问题**：`remember=True` 的部署里，git 慢一次就冻结整个事件循环最长 30 秒
  ——所有 socket、所有在跑的回合一起停。与本项目自身纪律直接矛盾：
  `read_file` 走 `to_thread`、`CliApprover` 走 `to_thread`、ruff ASYNC 规则常开。
- **修法**：`run_pipeline` 内的 `ensure_repo`/`hand_edits`/`commit`/
  `pending_sessions` 包 `asyncio.to_thread`；测试：monkeypatch 一个慢 git，
  断言事件循环期间心跳任务不被饿死。

### M3 — 同步 `def` 路由从 worker 线程并发读写无锁的共享表

- **位置**：`src/minicodex/web/routes.py` 的 `login`（:229）、`bootstrap`
  （:211）、`create_account`（:357）、`change_password`（:317）均为同步
  `def`（anyio worker 线程）；无锁表：`LoginThrottle._pairs`、
  `AccountStore`（读-改-写 accounts.json）、`SessionTable`、`Console.busy/running`
- **问题**：`store.py:171` 给 `Store` 配了 `threading.RLock`，更敏感的表反而
  没有。两个管理员并发建号是"都读到旧列表、都追加、后写覆盖先写"——丢账号。
  GIL 保证单步不崩，但读-改-写序列会丢。
- **修法**（二选一，倾向 a）：
  a. 给 `AccountStore._write`、`LoginThrottle`、`SessionTable` 的变更方法补
     `threading.Lock`（与 `Store` 对齐）；
  b. 把这几个路由改成 `async def`（scrypt 已在线程池外做不了——注意
     `hashlib.scrypt` 是阻塞调用，改 async 后需把 verify 丢 `to_thread`，
     改动面更大，故选 a）。

### M4 — 关键状态文件缺目录 fsync（`store.py` 有、更重要的三个文件没有）

- **位置**：`src/minicodex/web/accounts.py`（`_write`）、
  `web/sessions.py`（`_persist`）、`web/quota.py`（`_persist`）均为
  "写 tmp → fsync 文件 → replace"，没有随后的目录 fsync；
  `web/store.py:201-213` 有（且注释解释了 Windows 上的取舍）
- **问题**：断电时 rename 本身可能未持久。accounts.json 丢写的代价是全员登出
  （README-LIVE 自己给它的备份价值是"必须"）。
- **修法**：抽一个共享的 `atomic_write_json(path, payload)`（顺带消掉三处
  复制），含 store.py 同款的 best-effort 目录 fsync。

### M5 — `_AuditedShell` 退出码靠解析格式化字符串（formatting-as-interface）

- **位置**：`src/minicodex/web/runtime.py:303-318`（`_AuditedShell.run`，
  `:312` 起 `marker = "... (exit code "`）；约定源头
  `src/minicodex/shell.py:342`
- **问题**：从 shell 输出最后一行匹配 `"... (exit code N)"` 还原退出码。
  `shell.py` 改措辞、或命令输出本身以该模式结尾导致误解析时，审计日志
  **静默**记录错误退出码——对"宁停不谎"的审计链，静默错比缺失更糟。
  `test_live_audit.py:69-81` 把这个约定钉住了，说明作者知情，但结构化通道
  成本一行。
- **修法**：`ShellSession.run` 增加结构化出口——`self.last_exit: int | None`
  （在 `:342` 旁赋值，timeout/ceiling 路径为 None 或哨兵）；`_AuditedShell`
  读属性，删解析。补测试：命令输出伪造尾行 `"... (exit code 0)"` 时审计记录
  的是真实退出码。

---

## 三、低严重度（L）

### L1 — `/api/health` 暴露账号数，与自家匿名原则相抵

- **位置**：`src/minicodex/web/routes.py:139`；对照
  `web/auth.py:54-60`（status 端点刻意 `has_account` 布尔并解释"账号数量不是
  匿名调用方该知道的"）；`test_live.py:233-248` 把计数钉住了
- **问题**：两个公开端点对同一原则给出相反答案；账号数是攻击者判断 bootstrap
  是否关闭的信号。
- **修法**：health 改报 `has_account: bool`（或干脆删掉该字段）；同步改
  `test_LIVE_07_health_is_public_and_process_only`。

### L2 — 审计常量是死的 + docstring 承诺了没发生的事

- **位置**：`src/minicodex/web/audit.py:44-51`（`EVENT_*` 八个常量全项目零
  引用，调用方全用裸字符串；实际事件 `command_done` 无常量；`EVENT_DENIED`
  无人发出）；audit.py 模块 docstring 承诺记录
  "the refusals -- quota, throttle, workspace bounds"，而 `routes.py` 里配额
  （:664）、节流（:107）、workspace 越界（:469）三处拒绝**均未写审计**
- **修法**（两步，选一）：
  a. 兑现 docstring：三处拒绝各补一行 `audit.append("denied", "-", kind=...,
     detail=...)`，调用方改用常量；
  b. 或删掉 `EVENT_*` 与 docstring 里那半句，改成诚实清单。
  按本仓库"宁少承诺"的调性，倾向 a 里的拒绝记录 + 删多余常量。

### L3 — 私有名跨界导入

- **位置**：`src/minicodex/web/routes.py:44,531` 与 `web/events.py:29`
  从核心导入 `_dump_item`；核心侧 `rollout.py:187` 的 `_load_item` 同族
- **修法**：`rollout.py` 把 `_dump_item` 转正为 `dump_item`（保留旧名一个
  deprecation 别名一个版本），web 侧改导入。

### L4 — 死代码两处

- `src/minicodex/sandbox.py:273`（`chdir_is_reachable`，有测试无生产调用方；
  bwrap setup-failure 的启发式 `setup_failure` 已覆盖实际路径）
- `src/minicodex/rollout.py:586`（`items_of`，零调用方）
- **修法**：删（测试同步删）；或在 `sandbox.wrap` 的调用方接上
  `chdir_is_reachable` 预检（若认可其价值）。倾向删。

### L5 — `@app.on_event("shutdown")` 已废弃

- **位置**：`src/minicodex/web/app.py:218`
- **修法**：迁移到 `lifespan` 上下文管理器；`test_LIVE_06` 系列的
  `TestClient` 生命周期已能覆盖验证。

### L6 — `_AuditedShell` 四个属性装两个值

- **位置**：`src/minicodex/web/runtime.py:299-301`（`audit/actor` 给
  `tools.py:369-370` 的 `getattr` 读，`_audit/_actor` 自己用）
- **修法**：统一为 `audit/actor` 一对（`apply_patch` 的绑定改读同一对），删
  双份。若做了 M5，可顺路合并。

### L7 — probe 脚本散在仓库根目录

- **位置**：30+ 个 `probe_*.py` 在仓库根
- **问题**：作为部署 fork 是噪音；且 `test_packaging.py:242` 用
  `repo_root.glob("probe_mutations*.py")` 挂着弱耦合。
- **修法**：移入 `probes/`，同步改 `test_packaging.py`、
  `test_F23_02_the_probe_that_measured_this_is_in_the_step`、postmerge.yml 的
  run 行。纯移动，无行为变化。

### L8 — 可读性小项（一次 PR 顺路清）

- `sessions.py:23-33`：同一段 "The hash is a single SHA-256..." 原样出现
  两遍——删一段（复制粘贴痕迹，说明散文体量已超可维护临界）。
- `routes.py` 模块 docstring 的 `_busy`/`_running` 指向的实为
  `Console.busy/running`（app.py:115-118），名字过时。
- `MemoryError_`（memory.py:167，尾下划线避让内建）→ `MemoryLoadError`；
  同步 `__all__` 与导入方（`web/runtime.py`、`__main__.py`、测试）。
- `composition.py:255` / `:269` 的 `if memory is not None and memory and ...`
  用对象真值表达"非空" → 给 `Memory`/`Skills` 加 `non_empty` 属性。
- `__main__.py:100-102` 的 `CONSOLE_DATA_DIR` 一行式（两次读环境变量+条件
  拼接）→ 拆 4 行平铺。
- `sessions.py:132` / `quota.py:123` 用 `assert isinstance(...)` 做控制流再
  `except AssertionError` 捕获——`python -O` 下 assert 被剥离，安全网变死
  代码；改显式 `if not isinstance(...): raise/log`。
- 注释瘦身总策略：留在代码里的是"约束与不变量"（如 quota claim 的时序理由、
  sandbox 哨兵语义）；历史叙事与测量数据（agent.py:88-118 等）可迁 `docs/`；
  跨章引用（F12-03 等）改为 `docs/history.md` 锚点或删。**这条是方向性建议，
  动手前先定一页规范**，避免把 load-bearing 的注释误删。

---

## 四、结构改进（S）——"复制 + 测试对齐"升级为搬运

三处 `__main__` ↔ web 层的复制，`check_layers.py` 只禁 core→web 不禁
web→core，搬运是干净的出路（每条都已有相等性测试钉着，搬完改断言即可）：

- **S1 `_instructions`**：`__main__.py:105-163` 与 `web/runtime.py:98-131`
  行为逐字相同，靠 `test_faults_ch19.py` 的相等性断言看住。提示词组装是 core
  关注点，搬到 `composition.py` 或新 `prompting.py`，两边导入，复制体与漂移
  测试一并消掉。
- **S2 resume 块**：`__main__.py:288-310` ≈ `web/runtime.py:496-515`（读
  rollout、damage note、dropped note、environment note、forked_from）。同法。
- **S3 provider 种子表**：`web/store.py:49-62` 的 `SEED_PROVIDERS` 与
  `__main__.py:86-89` 的 `PROVIDERS` 靠测试对齐；`model.py`（base URL 的家）
  两边都能导入——把 provider 表搬过去。
- **S4 附带**：M4 的 `atomic_write_json` 抽取也属此列（三处复制）。

---

## 五、Recorder / RolloutWriter / History 辨析结论（保留，不动结构）

辨析背景：三者的"重复感"。结论：**不是重复，是同一次运行的三个投影**，从
循环两个不同点喂入，服务三个互不替换的消费方——结构应保留；真实问题是
命名与就近文档（见 D5）。

```
Agent 循环
 ├─ history.add_*()          ← 每个被接受的事实（不变量：每个 call 恰好被回答一次）
 │    └─ observer 钩子 ──→ RolloutWriter.append(item)   （逐条落盘，可 resume）
 └─ recorder.record(...)     ← 每次越过模型边界的事件（agent.py 直接调用，不经 History）
      request / response / model_failure（含 attempt 结构与失败证据）
```

关键区分（429 重试一例）：Rollout 里只有成功轮的条目——失败的尝试没有产生
被 History 接受的条目，**在会话文件里完全不存在**；Recorder 里则有完整的
attempt 序列、429 的 status/headers/body/request_id。反向：Rollout 独占
`meta`（版本化会话头）与 `mark`（resume 丢尾、压缩基线重置的依据），且其
条目能经 `_load_item` + `History.add_*` 重放回合法对话——recorder 的
wire 形状做不到。消费方：resume 读 rollout；replay/确定性测试读 recorder；
浏览器拿 `StreamingRolloutWriter` 的条目级投影。

不合并的两条契约理由：(1) 稳定性契约不同——rollout 是有版本迁移承诺的数据
格式，recorder 是"无稳定性承诺"的调试辅助；(2) 失败模型不同——Recorder 吞
OSError，RolloutWriter 在锁冲突时必须抛。

接受的成本（评审记录在案，不修）：Recorder 每轮全量重录 request（O(n²)，
replay.divergence 逐字节比较所需）；config 事件与 SessionMeta 双份
（F07-07 已知）。

---

## 六、与 codex 的机制差异对照（X）

对照基线：`../codex-main`（逐文件读过 `plan.rs`/`plan_tool.rs`/
`multi_agents*.rs` 全家/`apply_patch_spec.rs`/`unified_exec`/
`world_state/agents_md.rs`/`user_instructions.rs`/`compact.rs`/
`responses_retry.rs`/`error.rs`/memories 与 skills 常量）。分三类：
**A = 有意偏离（记录即可，不必改）**；**B = 哲学差异（双向成立）**；
**C = 值得对齐或至少补文档（含本报告已立案的 H1）**。

### X-A 有意偏离，自洽，保留

| # | 机制 | codex | minicodex | 依据 |
|---|---|---|---|---|
| A1 | **apply_patch 接口** | freeform grammar（`*** Begin Patch`），支持 Add/Delete File 与 move | JSON `{edits:[{path,old_text,new_text}]}`，仅替换 | freeform 需 Responses API custom tool；minicodex 走 chat completions 做不到。真实损失：无"新建文件"语义，可测后补 |
| A2 | **shell** | `unified_exec`：持久 PTY 会话、`exec_command(yield_time_ms)` + `write_stdin` | 每命令一个新子进程，`cd` Python 拦截，拒绝后台 `&` | 教学取舍已写明（shell.py docstring）；已知限制 `cd x && y` 不持久，已记档 |
| A3 | **compaction** | 替换式（`replace_compacted_history`，remote compaction、hooks、manual 触发） | 重放式（plan→cut→summary→History 重建，rollout marker） | append-only 历史的必然形状；`boundaries()`（真实 200/400 表）是本项目自有贡献 |
| A4 | **重试分类** | 语义枚举（`is_retryable` over `CodexErrorDetails`） | provider `code` 字符串表（`retry.py`） | chat-completions 生态没有语义枚举，等价能力。**codex 多一层传输回退（WS→HTTPS），minicodex 没有——记档，暂不补** |
| A5 | **ContextWindowExceeded 的反应** | 不重试，标记 full 返回，compact 是独立生命周期 | `_shrink` 自动压缩重试 | 场景适配：无人值守服务器 vs 有 UI 的交互；minicodex 的选择在其场景更合理 |
| A6 | **tool_search 触发面** | 全局机制（extension/plugin/MCP 均可 defer，`defer_loading` 是 ToolSpec 字段） | 仅 MCP 工具（local 永不 defer），token 预算触发 | 规模原因；机制一致。codex 另有 MCP 并行 opt-in（server opt-in + readOnlyHint），minicodex 一律 STATEFUL，更保守 |
| A7 | **记忆/技能常量** | — | 逐一核对**全部属实**：`SUMMARY_TOKEN_BUDGET=2500`（ext/memories/src/lib.rs:16）、`MAX_ROLLOUTS_PER_STARTUP=2`（config/types.rs:46）、`MIN_RATE_LIMIT=25%`（:49）、skills `MAX_DESCRIPTION=1024`（ext/skills/render.rs:21）、citation 格式 `<oai-mem-citation>`（read_path.md:75-80）、行为信号与 citation 信号分离（memories/read/src/usage.rs） | 这一块对照纪律全项目最好 |
| A8 | **skills 工具** | `skills.list/read`（authority/package/resource） | 默认 read_file 开 catalog 路径；`read_skill` 仅旗标后演示（F18-14 自认实测打平） | 注释已自我交代，诚实 |

### X-B 哲学差异：codex 口头规则 → minicodex 代码强制（双向成立，保留但确认声明准确）

| # | 规则 | codex | minicodex |
|---|---|---|---|
| B1 | 至多一个 `in_progress` | 只在 tool description 里说，代码不查（plan.rs 仅 parse+发事件） | 代码强制（plan.py `_parse`） |
| B2 | completed 前需有中间 work | **不存在**——连续两次 update、spawn 后立即全标 completed 均照收 | `work_since_update==0 且 updates>0` 拒绝 |
| B3 | 步数上限 | 无 | `MAX_STEPS=12` |
| B4 | 回显 | 固定 `"Plan updated"`（有 TUI 面板） | 回显完整计划+剩余数（无面板，靠历史文本延续） |
| B5 | loop 是否读计划 | 从不 | `unfinished_note` 停机前 nudge 一次 |

**定性**：codex 用提示词+模型自觉（它有服务端/面板兜底），minicodex 用代码
强制（append-only 历史、无面板、"计划写完立刻关"实测有害）。**注意 B1 的
引用是准确的**——plan.py:159-161 说的正是"codex states this rule and does
not check it"。偏离有测量依据且文档已标注，判为诚实偏离；MAX_STEPS 与
"无面板所以回显"两处 docstring 也已说明理由。无需改码，无需改文档。

### X-C 与上游的结构性分歧：subagent 协作模型（最大的一处，含 H1）

| 维度 | codex（multi_agents） | minicodex（spawn_agent） |
|---|---|---|
| 调用模型 | spawn **立即返回** `{agent_id, nickname}`，子代理后台并行；`wait_agent` 轮询（默认 30s，可 targets/timeout），未到返回 `timed_out` | spawn **阻塞到子代理跑完**，工具输出即最终文本（或带头的失败块） |
| 并发 | 一次 spawn 多个 + 一个 wait 全收；`max_concurrent_threads_per_session` 默认 4 | 顺序执行（spawn=STATEFUL，调度器不许并行） |
| 通信面 | spawn / wait / send_input(interrupt) / resume_agent / close_agent；v2 另有 followup_task 等 | 一个 spawn_agent，一次性 |
| 上下文 | 默认 **fork 全历史**（`fork_context`），或 role+环境精选；子显式继承 approval/sandbox/cwd/model（`apply_spawn_agent_runtime_overrides`） | **绝不继承历史**（7x token 测量）；只继承 cwd/timeout——**H1：sandbox 没继承** |
| 深度 | `agent_max_depth` 默认 **1**（"Solve the task yourself"） | `MAX_DEPTH=2` |
| 预算 | 深度+并发封顶 | 另有 `child_turn_budget=24`（宽度预算，codex 无对应物） |
| 回传 | 子完成后向父注入 `<subagent_notification>`（role=user JSON） | 结果即工具输出文本，失败块带 advice |
| 使用门槛 | 描述长篇："用户/AGENTS.md 明确要求才 spawn"、"不要 delegate 关键路径" | 描述短小 |

**定性**：不是同一机制的不同实现，是**两套协作模型**。codex 是后台并行
worker 池（依赖 agent_control 服务、跨会话线程管理）；minicodex 是同步嵌套
agent loop（`await child.run()`，单进程单事件循环）——阻塞式是保证 chapter 7
"每个 call 恰好一个输出"不变量的唯一形状，在其语境自洽。
**需要动的只有两件**：
1. **H1（代码）**——子代理沙箱/审计缺口，见上；
2. **D1（文档）**——`subagent.py` 没有任何一处明说"codex 的 spawn 是异步
   返回+wait 轮询、本实现是同步阻塞"。这是与上游最大的结构性差异，却在
   自称逐 feature 对照的文档里缺席。补一段到 `subagent.py` 模块 docstring
   （对照小节），同时把"codex 默认深度 1 vs 本实现 2"一并写明。

### X-D 其余对照发现（文档级别，并入 D 组）

- **D2 AGENTS.md 的 role**：codex 用 **role="user"** 的 ContextualUserFragment
  （`user_instructions.rs: role() -> "user"`），每轮 world_state diff 重发，
  替换文案 *"These AGENTS.md instructions replace all previously provided…"*；
  minicodex 用 role="developer" + append-only 说一次（F13-12 测量支撑：
  对抗 fortified system prompt 时 developer 3/3 vs user 1/3）。
  **需补文档**：`agents_md.py` 模块 docstring 未明说 codex 是 user role +
  per-turn 替换（只在 DeveloperNote docstring 里带过）。
- **D3 传输层回退**：codex 有 WS→HTTPS transport fallback
  （`responses_retry.rs:33-45`）；minicodex 单通道。记档即可（A4 已提）。
- **D4 freeform patch 的 Add/Delete File**：minicodex 无法新建文件（A1 的
  真实功用差距）。可选跟进：给 `apply_patch` 加 `old_text: ""` = 新建文件的
  语义（先测后加，本仓库规矩）。

---

## 七、文档债汇总（D，独立于代码修复）

| 编号 | 内容 | 位置 |
|---|---|---|
| D1 | subagent 同步阻塞 vs codex 异步 spawn+wait 的结构性差异未声明；codex 默认深度 1 未提 | `subagent.py` 模块 docstring |
| D2 | AGENTS.md 的 role=user + 每轮替换 vs developer + 说一次，对照未在 `agents_md.py` 就近声明 | `agents_md.py` |
| D3 | 传输层回退缺失未记档 | README-LIVE"明确还不有什么"或 `retry.py` |
| D5 | `recorder.py` 自己的 docstring 只说 "Deliberately dumb"，未提与 rollout 的分工——新读者打开的第一个文件恰恰是唯一没被告知区别的（rollout.py 和 replay.py 都解释了，recorder.py 没有） | `recorder.py` docstring |
| D6 | audit docstring 承诺的 refusals 未兑现（同 L2） | `audit.py` |
| D7 | `runtime.py:277` 注释声称 sub-agent seeding 读三个属性，实际只读两个（同 H1） | `web/runtime.py` |

---

## 八、建议修复顺序

| 批次 | 内容 | 预估 |
|---|---|---|
| 1 | H1（子代理沙箱+审计）+ H2（confined 接线）+ D7 同步改注释 | 半天 |
| 2 | H3（accounts 回退）+ H4（throttle 泄漏）——两个正确性洞，各一两行 + 测试 | 2 小时 |
| 3 | M5（last_exit）→ M1（CLI 沙箱）→ M2（to_thread）→ M3（锁）→ M4（原子写抽取） | 1 天 |
| 4 | S1–S4 搬运（消复制） | 半天 |
| 5 | L1–L8 + D1–D7 文档批 | 半天 |
| 6 | X-C 的 subagent 对照补写进 docstring；A 组其余仅记录，不动 | 1 小时 |

每批独立可交付；批次 1、2 上线优先（H1 是安全承诺被撤销，H3 是 bootstrap
令牌可被争取）。

---

*评审方法注记：本清单来自对 `src/`、`tests/`、前端、部署栈的逐行阅读，
codex 侧结论逐条到文件:行号核验（含 uvicorn `FORWARDED_ALLOW_IPS`、
`proxy_headers` 默认值等部署疑点的实证）。行号锚点在撰写时逐个 grep 复核过；
若 main 前进导致漂移，以函数/常量名为准。*
