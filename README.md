# 从零复刻 Codex

用 Python 从零写一个编码 Agent，结构与 OpenAI codex 同构。

不是"跟着敲一遍就能跑"的教程。是**一个开发者的迭代日志**：每一章从一个具体的、
看得见的目标开始，把它拆成待办，一段一段写出来、跑起来、撞上问题、修好，
然后用职业工程师的方式交付。

## 这本教程和别的有什么不一样

**三条主线并行，不偏科。** 每一章都同时推进三件事：
（A）**怎么把需求想清楚并写成代码**——拆解目标、判断该不该抽象；
（B）**怎么用工程化的方式交付**——分支、commit、PR、review、CI；
（C）**怎么预防和处理故障**——全书 245 条，每条有编号和可复现的测试。

**故障是做事的结果，不是章节的开头。** 每一章先做事，撞上问题，再回头编号存档。
245 条里只有 24 条会自己报错，剩下 90% 悄无声息。见 [`FAULTS.md`](FAULTS.md)。

**代码是跑过的。** 教程里贴出的每一段终端输出都是真实运行结果。每一个修复都做过变异
验证：把补丁改回去，确认对应的测试确实会红。

**不用查源码。** codex 的相关机制会在每章末尾对照说明，但你不需要打开它才能跟上。

## 怎么用

```bash
cd steps/step-1_setup
uv sync --all-extras
uv run minicodex --version
uv run pytest
```

每个 `steps/stepXX/` 都是自包含、可独立运行的完整工程，可以从任意章节切入。

## 目录

| 步骤 | 章节 | 主题 |
|---|---|---|
| `step-1_setup` | [Ch-1](tutorial/ch-1-setup.md) | 把一个想法变成一个能装的东西 |
| `step00_minimal_loop` | [Ch00](tutorial/ch00-minimal-loop.md) | 让它先去查，再回答 |
| `step01_protocol` | [Ch01](tutorial/ch01-protocol.md) | 先定协议，再写逻辑 |
| `step02_shell_tool` | [Ch02](tutorial/ch02-shell-tool.md) | 第一个 shell 工具 |
| `step03_tool_descriptions` | [Ch03](tutorial/ch03-tool-descriptions.md) | 工具描述工程：说清楚它什么时候**别**用 |
| `step04_apply_patch` | [Ch04](tutorial/ch04-apply-patch.md) | 改文件：锚定，不是行号 |
| `stepA_refactor` | [插曲 A](tutorial/chA-refactor.md) | 第一次重构：该拆的和不该拆的 |
| `step05_approval` | [Ch05](tutorial/ch05-approval.md) | 审批与沙箱：不认识的东西默认是问题 |
| `step06_compaction` | [Ch06](tutorial/ch06-compaction.md) | 上下文压缩：删掉之后，剩下的还知道自己在干什么吗 |
| `step07_resume` | [Ch07](tutorial/ch07-resume.md) | 中断与恢复：活过自己这个进程 |
| `step08_concurrency` | [Ch08](tutorial/ch08-concurrency.md) | 工具并发：一轮里三个调用，能不能同时跑 |
| `step09_mcp` | [Ch09](tutorial/ch09-mcp.md) | 工具太多：用别人写的工具，从别人的进程里 |
| `step10_subagents` | [Ch10](tutorial/ch10-subagents.md) | 子 Agent：一个工具调用的内部，是另一整轮对话 |
| `stepB_boundaries` | [插曲 B](tutorial/chB-boundaries.md) | 第二次重构：打断循环依赖，把边界写成 CI 检查 |
| `step11_plan` | [Ch11](tutorial/ch11-plan.md) | 任务拆解与 plan：在模型宣布做完之前，先写下什么叫做完 |
| `step12_retry` | [Ch12](tutorial/ch12-retry.md) | 重试与错误分类：这次失败是哪一种 |
| `step13_system_prompt` | [Ch13](tutorial/ch13-system-prompt.md) | 系统提示词：缓存、角色选择、以及 `AGENTS.md` 静态约定层 |
| `step14_eval` | [Ch14](tutorial/ch14-eval.md) | 评估与回归：把一次真实运行变成离线的确定性测试 |
| `step16_memory_read` | [Ch16](tutorial/ch16-memory-read.md) | 记忆（一）：先手写一份，证明读它有用 |
| `step17_memory_write` | [Ch17](tutorial/ch17-memory-write.md) | 记忆（二）：让它自己写，并且学会忘 |
| `step18_skills` | [Ch18](tutorial/ch18-skills.md) | 技能：只把书脊放进提示词，正文等它自己来翻 |
| `step19_web_console` | [Ch19](tutorial/ch19-web-console.md) | Web 控制台：换一个人机界面，Agent 一行都不用改 |
| `step20_sandbox` | [Ch20](tutorial/ch20-sandbox.md) | 沙箱：把"允许"从一个判断变成一条边界 |
| `step15_release` | [Ch15](tutorial/ch15-release.md) | 发布与维护：交出去之后，它就不再只属于你 |
| `step21_mcp_remote` | [Ch21](tutorial/ch21-mcp-remote.md) | 连到别人的服务器：MCP 的远程传输、认证与资源 |
| `step22_mcp_console` | [Ch22](tutorial/ch22-mcp-console.md) | 控制台里连一个服务器：浏览器中介的 OAuth |
| `step23_multi_user` | [Ch23](tutorial/ch23-multi-user.md) | 谁在用这个控制台：认证、会话、归属与配额 |

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
其余 23 个单元不需要 Node，这三章的后端和全部 1939 个 Python 测试
也不需要。前端测试（vitest）到 Ch23 一共 9 个，那 9 个确实要 Node——
而它们在 Ch22 那一整章里从来没有在 CI 上跑过，这是 Ch23 找出来的
F23-10。

**Ch20 的沙箱要 Linux。** 在 Windows 或 macOS 上，那一章的代码照样测得动
（它断言生成的 bwrap 命令行），但真正的隔离只有 Linux 内核给得了；
`probe_sandbox.py` 在别的平台上会明说自己什么都没测出来，而不是报一个绿色的成功。
