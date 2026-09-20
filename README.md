# 从零复刻 Codex

用 Python 从零写一个编码 Agent，模块结构参照 OpenAI Codex。

不是"跟着敲一遍就能跑"的教程。是**一个开发者的迭代日志**：每一章从一个具体的、
看得见的目标开始，把它拆成待办，一段一段写出来、跑起来、撞上问题、修好，
然后用职业工程师的方式交付。

## 这本教程和别的有什么不一样

**三条主线并行，不偏科。** 每一章都同时推进三件事：
（A）**怎么把需求想清楚并写成代码**——拆解目标、判断该不该抽象；
（B）**怎么用工程化的方式交付**——分支、commit、PR、review、CI；
（C）**怎么预防和处理故障**——全书 259 条，每条有编号，成立的有可复现的测试。

**故障是做事的结果，不是章节的开头。** 每一章动工前先列出可能出的问题，写完再逐条
验证：成立的修掉并用测试钉住，**不成立的也留着**——259 条里有 27 条是这样的负结果，
连同证据一起保存，而不是删掉。

259 条里只有 24 条会在发生的当下抛异常，其余 91% 不会——错的答案、慢的答案、更差的
答案，或者什么都不发生，直到用户来抱怨。见 [`FAULTS.md`](FAULTS.md)。

**代码是跑过的。** 教程里贴出的每一段终端输出都是真实运行结果。每一个修复都做过变异
验证：把补丁改回去，确认对应的测试确实会红。

**不用查源码。** codex 的相关机制会在每章末尾对照说明，但你不需要打开它才能跟上。

## 仓库里有什么

```
tutorial/         27 章正文
steps/            27 个自包含、可独立运行的工程快照，每章一个
FAULTS.md         259 条编号故障档案（含 27 条负结果）
minicodex-live/   把最后一章做成可部署的服务
```

前三者是**推导过程**：怎么想、怎么验、以及哪些猜错了。

[`minicodex-live/`](minicodex-live/) 是**交付结果**：从最后一章的快照出发，补上
一个真能部署的服务所需要的东西——PostgreSQL 持久化与迁移、登录限流、审计日志、
Docker 与 nginx，以及一轮完整代码评审后的修复（清单见
[`minicodex-live/REVIEW-FINDINGS.md`](minicodex-live/REVIEW-FINDINGS.md)）。

想看"怎么一步步长出来的"，从 `tutorial/` 读起；想看"最后交付成什么样"，
直接进 `minicodex-live/`。

## 这些数字是怎么数出来的

会被查的三个数，先说清楚，省得你自己去数：

| 说法 | 怎么核 |
|---|---|
| 27 章教程，每章一个可运行快照 | `ls tutorial/*.md` 与 `ls steps/` 各 27，一一对应 |
| 259 条故障档案 | `grep -cE '^\| (✅\|⬜\|—) ' FAULTS.md` |
| 其中 24 条会崩溃 | `grep -cE '^\| (✅\|⬜\|—) .*🔴' FAULTS.md` |

两点容易误读：

- **259 不等于"我踩了 259 个坑"。** 每章动工前先列出可能出的故障，写完再逐条验证。
  成立的修掉并用测试钉住；**27 条怎么也复现不出来**，连同证据留下，没有删。
  `grep -c "NOT REPRODUCED\|Cannot arise" FAULTS.md` 会给出 28，多的那行是正文说明。
- **测试总数不是固定值。** `steps/*/tests/test_properties.py` 是种子驱动的属性测试，
  用例数由 `MINICODEX_PROPERTY_CASES` 决定（默认 200，CI 用 2000）。裸跑 `pytest`
  报出来的数会随这个变量变，所以本文不引用某个具体总数。

## 这份代码是怎么写出来的

AI 深度参与了实现。**三件事是我的**：模块怎么切、故障怎么定位、以及每一处
"为什么不选那个看起来更诱人的方案"——后者在源码注释和 `FAULTS.md` 里都留了痕迹。
如果你想验货，翻 `FAULTS.md` 里任何一条负结果：它记录的是一次没有得到预期结果的
实验，包括几次"没复现出来，但我这个实验本身不足以下结论"。

## 这本教程没做什么

写清楚边界，剩下的话才可信。

- **单人单分支，所以协作类故障在这里复现不出来。** 合并冲突、并发编辑这类问题
  结构上不可能发生，档案里如实记成了负结果（`FA-03`），没有假装验证过。
- **部分测量的样本量偏小。** 第 18 章有一次：8 次运行给出 `read_file` 8/8 对
  `read_skill` 6/8，第二次重测变成 10/12 对 11/12——结论直接反转。教训写进了
  档案（"每个数字旁边写上样本量，两轮合并 20 次再下笔"），但早期章节的一些结论
  仍然建立在小样本上。
- **属性测试没有 shrinking。** `test_properties.py` 用的是自己写的种子生成器，
  不依赖 hypothesis；代价是失败时只打印种子，不给最小反例，复现靠
  `MINICODEX_PROPERTY_SEED=<n>`。这个取舍写在文件头。
- **沙箱只有在 Linux 上才是真的。** Windows 和 macOS 上第 20 章照样测得动
  （断言生成的 bwrap 命令行），但真正的隔离只有 Linux 内核给得了。
  `probe_sandbox.py` 在别的平台会明说自己什么都没测出来，而不是报一个绿色的成功。
- **绝大部分章节跑在可回放的假模型上。** 需要真实模型的测量单独放在探针脚本里，
  结论归档在 minicodex-live 的 `docs/history.md`。
- **已知的洞不藏着。** 例如第 20 章判断沙箱启动失败靠的是 `bwrap: ` 前缀，
  一个自身输出就以 `bwrap: ` 开头的命令能骗过它；`--info-fd` 是更好的答案，
  留作练习，而不是当作不存在。

## 怎么用

每个 `steps/stepXX/` 都是自包含、可独立运行的完整工程，可以从任意章节切入。

**跑一章的测试**（离线，不需要 API key）：

```bash
cd steps/step09_mcp
uv sync --all-extras
uv run pytest
```

**验证这些测试不是摆设。** 一套全绿的测试什么也证明不了，除非你见过它因为
你的缘故变红。变异探针会把源码改坏——每次一行、都是真实故障的形状——跑一遍
测试，再把文件还原：

```bash
uv run python probe_mutations_ch09.py
```

每个变异后面会打印有多少个测试因它而失败。**有变异活下来（0 个测试发现），
脚本就以 1 退出并点名是哪一个**——那说明测试有洞。15 个章节带这样的探针
（`probe_mutations_ch*.py`）。

## 目录

**表的顺序就是阅读顺序。章节号是写作编号，两者只有一处不同：**
第 15 章排在第 20 章之后，因为它要发布和维护的东西里，包含第 16–17 章
才引入的记忆机制——按编号读会读到一堆还不存在的功能。故障编号（`F15-xx`）
跟着章节走，不跟着阅读序走，`FAULTS.md` 的排列与本表一致。

| 序 | 步骤 | 章节 | 主题 |
|---|---|---|---|
| 1 | `step-1_setup` | [Ch-1](tutorial/ch-1-setup.md) | 把一个想法变成一个能装的东西 |
| 2 | `step00_minimal_loop` | [Ch00](tutorial/ch00-minimal-loop.md) | 让它先去查，再回答 |
| 3 | `step01_protocol` | [Ch01](tutorial/ch01-protocol.md) | 先定协议，再写逻辑 |
| 4 | `step02_shell_tool` | [Ch02](tutorial/ch02-shell-tool.md) | 第一个 shell 工具 |
| 5 | `step03_tool_descriptions` | [Ch03](tutorial/ch03-tool-descriptions.md) | 工具描述工程：说清楚它什么时候**别**用 |
| 6 | `step04_apply_patch` | [Ch04](tutorial/ch04-apply-patch.md) | 改文件：锚定，不是行号 |
| 7 | `stepA_refactor` | [插曲 A](tutorial/chA-refactor.md) | 第一次重构：该拆的和不该拆的 |
| 8 | `step05_approval` | [Ch05](tutorial/ch05-approval.md) | 审批与沙箱：不认识的东西默认是问题 |
| 9 | `step06_compaction` | [Ch06](tutorial/ch06-compaction.md) | 上下文压缩：删掉之后，剩下的还知道自己在干什么吗 |
| 10 | `step07_resume` | [Ch07](tutorial/ch07-resume.md) | 中断与恢复：活过自己这个进程 |
| 11 | `step08_concurrency` | [Ch08](tutorial/ch08-concurrency.md) | 工具并发：一轮里三个调用，能不能同时跑 |
| 12 | `step09_mcp` | [Ch09](tutorial/ch09-mcp.md) | 工具太多：用别人写的工具，从别人的进程里 |
| 13 | `step10_subagents` | [Ch10](tutorial/ch10-subagents.md) | 子 Agent：一个工具调用的内部，是另一整轮对话 |
| 14 | `stepB_boundaries` | [插曲 B](tutorial/chB-boundaries.md) | 第二次重构：打断循环依赖，把边界写成 CI 检查 |
| 15 | `step11_plan` | [Ch11](tutorial/ch11-plan.md) | 任务拆解与 plan：在模型宣布做完之前，先写下什么叫做完 |
| 16 | `step12_retry` | [Ch12](tutorial/ch12-retry.md) | 重试与错误分类：这次失败是哪一种 |
| 17 | `step13_system_prompt` | [Ch13](tutorial/ch13-system-prompt.md) | 系统提示词：缓存、角色选择、以及 `AGENTS.md` 静态约定层 |
| 18 | `step14_eval` | [Ch14](tutorial/ch14-eval.md) | 评估与回归：把一次真实运行变成离线的确定性测试 |
| 19 | `step16_memory_read` | [Ch16](tutorial/ch16-memory-read.md) | 记忆（一）：先手写一份，证明读它有用 |
| 20 | `step17_memory_write` | [Ch17](tutorial/ch17-memory-write.md) | 记忆（二）：让它自己写，并且学会忘 |
| 21 | `step18_skills` | [Ch18](tutorial/ch18-skills.md) | 技能：只把书脊放进提示词，正文等它自己来翻 |
| 22 | `step19_web_console` | [Ch19](tutorial/ch19-web-console.md) | Web 控制台：换一个人机界面，Agent 一行都不用改 |
| 23 | `step20_sandbox` | [Ch20](tutorial/ch20-sandbox.md) | 沙箱：把"允许"从一个判断变成一条边界 |
| 24 | `step15_release` | [Ch15](tutorial/ch15-release.md) | 发布与维护：交出去之后，它就不再只属于你 |
| 25 | `step21_mcp_remote` | [Ch21](tutorial/ch21-mcp-remote.md) | 连到别人的服务器：MCP 的远程传输、认证与资源 |
| 26 | `step22_mcp_console` | [Ch22](tutorial/ch22-mcp-console.md) | 控制台里连一个服务器：浏览器中介的 OAuth |
| 27 | `step23_multi_user` | [Ch23](tutorial/ch23-multi-user.md) | 谁在用这个控制台：认证、会话、归属与配额 |

> 编号说明：Ch16/Ch17/Ch18/Ch19/Ch20 的**阅读顺序**在 Ch15 之前，**编号**在其之后。
> 这是为了不重排既有故障 ID——`F11-01` 已经写进代码、测试和本书正文，
> 重排会让"编号可反查"这条机制作废。Ch21、Ch22 和 Ch23 的阅读顺序和编号都在
> Ch20 之后，Ch23 是全书真正的最后一章。
>
> **26 个单元已全部写完。** Ch15 是收尾章：它测的不是程序，是**产物**——
> 一个 wheel、一份 `--version`、以及本书自己那十四个装好的历史版本写在磁盘上的文件。
>
> **Ch20 多一条环境说明**：它的沙箱是 Linux-only（bubblewrap）。
> 那一章 48 个测试里有 46 个在任何平台离线跑——因为它们断言的是**生成的命令行**，
> 不是内核行为；剩下 2 个需要真的 `bwrap`，跳过时会说明缺的是它而不是平台。
>
> **Ch21 全部离线跑。** `probe_mcp_remote.py` 另外有四段碰真实网络——两个公开
> MCP 端点，只读、未认证；其中一段需要一个真实的 GitHub token 才有意义，
> 没有就体面跳过而不是报错。
>
> **Ch22 的 GitHub 连接分两半，此处不包装。** 粘一个 personal access token
> 现在就能用；完整的交互式 OAuth 还需要运行控制台的人先自己注册一个
> GitHub OAuth App（GitHub 不广播 `registration_endpoint`，Ch21 对真实服务器量过）。
> 那一步按钮消不掉，表单里写明了。完整链路是对着假授权服务器证的，
> 对真实 GitHub 的交互式 OAuth **没有**跑过。
>
> **Ch23 给控制台加了登录，也如实说了它还不是什么。** 本地账号 + 口令
> （`hashlib.scrypt`，不加依赖、不自造 KDF）+ 会话 cookie；第一个账号靠
> `serve` 打印的一次性令牌创建。**没有** TLS 终止、**没有**登录节流、
> **没有**审计日志，配额算的是回合数而不是 token（理由和要改什么，正文
> 第 12 节写了）。默认仍然只监听 `127.0.0.1`：登录决定谁可以驱动 agent，
> 绑定地址决定谁够得到这个端口，有了前者不是撤掉后者的理由。

完整规划见 [`../PLAN.md`](../PLAN.md)。

## 每个 step 里有什么

| 文件 | 用途 |
|---|---|
| `src/minicodex/` | 该阶段的完整代码 |
| `tests/test_faults_chNN.py` | 每条故障一个测试，函数名带故障编号 |
| `README.md` | 这一步能干什么、怎么跑 |

提交序列、PR 描述、code review 意见都直接写在教程正文的工程化小节里，
和当章的技术内容放在一起读。

## 环境要求

Python ≥ 3.10（推荐 3.11+）、[uv](https://docs.astral.sh/uv/)、git。
不需要 API key——绝大部分章节跑在可回放的假模型上。

运行时依赖只有两个：`httpx`（跟模型说话）和 `mcp`（官方 MCP SDK，
从 Ch09 起）。这本书的分界线是**教什么就手写什么，不教的就依赖它**——
HTTP 和 MCP 协议都不是它教的东西，codex 也一样用官方 SDK
（`rmcp = "=3.0.0"`）。

**Ch19、Ch22 和 Ch23 多一条**：它们的前端是 React + Vite，要 Node ≥ 20。
其余 23 个单元不需要 Node，这三章的后端和全部 Python 测试
也不需要。前端测试（vitest）到 Ch23 一共 9 个，那 9 个确实要 Node——
而它们在 Ch22 那一整章里从来没有在 CI 上跑过，这是 Ch23 找出来的
F23-10。

**Ch20 的沙箱要 Linux。** 在 Windows 或 macOS 上，那一章的代码照样测得动
（它断言生成的 bwrap 命令行），但真正的隔离只有 Linux 内核给得了；
`probe_sandbox.py` 在别的平台上会明说自己什么都没测出来，而不是报一个绿色的成功。

## License

[MIT](LICENSE)
