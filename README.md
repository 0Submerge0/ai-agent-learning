# 从零手写一个 AI Agent（V0 → V5）

一个**不用任何 agent 框架**、只用 `requests` 手写 HTTP 调用的小型 AI agent 学习项目。

目标不是尽快做出成品，而是通过**逐版加一个能力**，把 agent 的运行机制拆开看清楚：模型到底做了什么、你的代码做了什么、工具是怎么被调用的、记忆存在哪、权限边界在哪。

> 适合：会一门编程语言（尤其是 Java）、想弄懂 AI agent 内部怎么转的人。
> 不适合：想直接找一个生产级框架的人——这里刻意不引入 LangChain / CrewAI / 官方 SDK。

---

## 它现在能做什么

到 V5 为止，它是一个**会先提交计划、等你批准、再逐步执行、断了能接着做**的命令行助手：

- 多轮对话，记得你之前说过什么（`/clear` 可清空记忆）
- 自己决定要不要调用工具、调哪个、传什么参数
- 能列目录、读文件、建目录、移动文件、按模式搜文件、写文本文件
- **先规划、你批准、再执行**：计划是代码里的 `Plan` 对象，不是一段文字
- 执行时逐项更新任务状态（`pending / in_progress / done / failed / skipped`）
- **会话可存档可恢复**：`/resume` 接着上次的进度做，不用重来
- **只能操作 `workspace/` 目录**，越界直接拒绝
- **两道闸门**：计划审批 `y/N` + 每个写操作 `y/N`
- 每次会话写 JSONL 日志（`logs/`），并按 token 计预算（默认 20000，V5 建议 80000）

---

## 版本路线

| 版本 | 文件 | 新增能力 | 要讲清的核心概念 |
|---|---|---|---|
| **V0** | `src/v0_chat.py` | 一次问答 | HTTP 请求结构、`messages` 数组、`system`、token 用量、为什么模型没有记忆 |
| **V1** | `src/v1_tool.py` | 单工具调用 | JSON Schema、`tools` 定义、`stop_reason=tool_use`、`tool_result` 回填 |
| **V2** | `src/v2_repl.py` | 多轮对话 + 记忆 | 历史累积与全量重发、`/clear`、上下文成本、错误重试 |
| **V3** | `src/v3_files.py` | 文件整理 | 沙箱与路径校验、写操作确认、dry-run、按职责拆包 |
| **V4** | `src/v4_assistant.py` | 多工具 + 简单规划 + 日志 + 成本控制 | 工具选择策略、system prompt 如何驱动规划、JSONL 可观测性、token 预算 |
| **V5** | `src/v5_planner.py` | 显式计划 + 计划审批 + 断点恢复 | 计划作为数据结构、阶段化工具集、两道闸门、原子存档与状态恢复 |

每一版的讲义在 [`docs/notes/`](docs/notes/)：

- [V0：一次问答](docs/notes/v0.md)
- [V1：模型自己决定调工具](docs/notes/v1.md)
- [V2：多轮对话与记忆](docs/notes/v2.md)
- [V3：文件整理、沙箱与「人把关」](docs/notes/v3.md)
- [V4：多工具、任务规划、日志与成本控制](docs/notes/v4.md)
- [V5：显式计划、两道闸门、断点恢复](docs/notes/v5.md)

---

## 快速开始

### 环境

- **Python 3.14**，用 [uv](https://github.com/astral-sh/uv) 管理解释器和依赖
- 依赖只有 `requests`（见 `pyproject.toml`）

```powershell
uv sync
```

### 配置 API Key

Key **只从环境变量读取**，不会写进任何文件：

```powershell
$env:ARK_API_KEY = "你的火山方舟 API Key"
```

`.env.example` 只是说明文件，程序不会读取它；里面的 `ARK_API_KEY=你的key` 是占位符。

### 运行

```powershell
# V0：一次问答
uv run python src/v0_chat.py "用一句话解释什么是 agent"

# V1：让模型自己决定调用工具
uv run python src/v1_tool.py "看看当前目录里有什么文件"

# V2：多轮对话（记住你说过什么）
uv run python src/v2_repl.py

# V3：文件整理（只能操作 workspace，写操作要确认）
uv run python src/v3_files.py

# V4：多工具助手（先勘察、再计划、再执行；带日志与预算）
uv run python src/v4_assistant.py

# V5：显式计划助手（先规划、你批准、再执行；可 /resume 接着做）
uv run python src/v5_planner.py

# 可选：给 V4 换一个更小的预算，演示「预算用完就停」
$env:MAX_SESSION_TOKENS = "800"
# V5 要跑两遍（规划 + 执行），实测一次完整流程约 63k tokens，建议给足
$env:MAX_SESSION_TOKENS = "80000"
```

V2 / V3 / V4 / V5 是交互式 REPL：

```
你> 把 workspace 里的图片都移动到 images 目录
助手> （先列计划，问你是否确认）
你> y
[计划] 创建目录 workspace/images
执行? (y/N) y
...
/clear   # V2/V3 清空记忆；V4 同时重置预算；V5 清空记忆+计划+预算（不删存档）
/plan    # V5：查看当前计划与进度
/resume  # V5：恢复最近一次会话存档
/exit    # 退出
```

V4 / V5 启动时还会打印本次日志路径，例如 `logs/session-20261110-115235.log`。

---

## 模型接入

项目使用火山方舟 Coding Plan 的 **Anthropic Messages API** 格式：

| 项 | 值 |
|---|---|
| 地址 | `https://ark.cn-beijing.volces.com/api/coding/v1/messages` |
| 请求头 | `x-api-key`、`anthropic-version: 2023-06-01` |
| 模型 | `deepseek-v4-1-flash-260910` |

注意：是 **Anthropic Messages API**，不是 OpenAI Chat Completions 格式。

---

## 代码结构

```
src/
  v0_chat.py          # V0：单轮对话
  v1_tool.py          # V1：单工具调用
  v2_repl.py          # V2：多轮 REPL + 记忆
  v3_files.py         # V3：文件整理入口
  v4_assistant.py     # V4：多工具 + 规划 + 日志 + 预算入口
  v5_planner.py       # V5：显式计划 + 审批 + 断点恢复入口
  agent/              # V3 起抽出的公共包
    client.py         #   和模型通信（发请求、重试、解析）
    tools.py          #   V3 的 4 个工具（V4/V5 不改它，保留快照）
    sandbox.py        #   路径校验（只能操作 workspace）
    tools_v4.py       #   V4 的 6 个工具 + 日志摘要
    tools_v5.py       #   V5：阶段化工具集（规划只读 / 执行可写）
    plan.py           #   V5：Task / Plan 状态机
    session_store.py  #   V5：会话原子存档（供 /resume）
    logger.py         #   JSONL 会话日志
    budget.py         #   token 预算（V5 加了 to_dict/restore）

workspace/            # agent 唯一被允许改动的目录
logs/                 # 会话日志（已 gitignore）
sessions/             # 会话存档，含对话内容（已 gitignore）
docs/
  notes/              # v0.md ~ v5.md：每版讲义
  superpowers/
    specs/            # 设计文档
    plans/            # 每版实现计划
```

**为什么 V0/V1/V2 是独立单文件，V3 才拆包？** 因为抽象是被需求逼出来的：前三版单文件还能并排对照，V3 加了 4 个工具 + 沙箱 + 确认后，一个文件就读不清了。拆包不是「为了像样」，是「不拆就看不懂了」。

---

## 安全设计（V3 起）

### 1. 沙箱

所有文件操作只能发生在 `workspace/` 之内。实现是 `agent/sandbox.py` 里的 `safe_path()`：

```python
target = (p if p.is_absolute() else base / p).resolve()
if not target.is_relative_to(base):
    raise ValueError("路径越界")
```

`resolve()` 会把 `..` 和符号链接折回真实位置，所以 `../src`、绝对路径、软链接穿透都会被拒绝。

### 2. 两道闸门：先批计划，再批动作

V5 起，动手之前有两道独立确认：

**第一道，计划审批**（`v5_planner.py`）：规划阶段结束后打印整份计划，只有输入 `y` 才进入执行；回车或其他输入一律当作拒绝，磁盘不会发生任何变化。

```
批准这个计划? (y/N) y
```

**第二道，写操作确认**（`tools_v4.py`）：`mkdir` / `move_file` / `write_file` 默认只打印计划，用户输入 `y` 才执行：

```python
WRITE_TOOLS = WRITE_TOOLS_V3 | {"write_file"}

if name in WRITE_TOOLS:
    print(f"[计划] {describe_write_v4(name, args)}")
    if input("执行? (y/N) ").strip().lower() != "y":
        return "用户拒绝了这次操作，未执行。"
```

为什么分开：批计划回答「这件事要不要做」，批写操作回答「这一步现在能不能落盘」。合成一道，等于批了一份 7 步计划就授权 20 次磁盘写入。

`write_file` 还会显示内容预览（前 40 字符），并且目标已存在时必须显式传 `overwrite=true`，否则拒绝覆盖。

### 3. 阶段隔离：规划阶段没有写工具

规划阶段发给模型的工具清单里只有 `list_dir` / `read_file` / `search_files` / `create_plan`，执行阶段才有 6 个写工具。模型自己编一个工具名也调不出来——分发函数会明确返回「规划阶段不能调用 write_file」。权限边界写在代码里，不写在提示词里。

### 4. 不提供 shell

本阶段**不提供** `run_shell` 工具。沙箱的保护范围 = 你提供的工具集合；一旦能执行任意命令，应用层路径检查就不够了，需要升级到容器/虚拟机级别的隔离。

### 5. Key 不进文件

API Key 只从 `ARK_API_KEY` 环境变量读取，`.env` 已被 `.gitignore` 忽略。

> 如果这个 key 曾经出现在任何提交或公开位置，请立即去火山方舟控制台作废并重新生成。

### 6. 日志与存档不泄密

V4/V5 的日志写到 `logs/`（已 gitignore）。`user_message` 只记字符数、不记原文；`write_file` 的参数只记 `path / chars / overwrite`，不记全文。日志用于事后查「做了什么、花了多少」，不是用来存档聊天内容。

V5 的会话存档写在 `sessions/`（已 gitignore）。它**必须**保存完整 `messages`——否则恢复出来的模型就失忆了。所以它比日志敏感得多：目录不提交、不分享。日志里也不会出现 API Key（key 只从环境变量读，从不进 messages）。

---

## 设计原则

1. **不用框架**：agent 的骨架很小——「问模型 → 执行工具 → 结果回喂 → 再问」，自己写一遍才能真正理解。
2. **一次只加一个概念**：每版都能独立跑通，且能说清「这一版新增的 X 行解决了什么问题」。
3. **抽象等需求逼出来**：V1/V2 刻意不拆包，V3 才拆。
4. **错误信息是给模型看的**：工具报错返回可读文字，而不是让程序崩——模型据此自我纠正。
5. **危险操作要有人把关**：模型可以提议，但不能单方面执行不可逆操作。
6. **状态要落盘，不能只活在内存里**：V5 的每一步都写 `sessions/`，断了能接着做——「可恢复」不是附加功能，是长任务的必需品。

---

## 说明

这是一个**个人学习项目**，不是生产级工具。它的价值在于「每一行都能解释为什么」，而不在于功能完整或性能。

如果你也想走一遍这条路，建议按 `docs/notes/` 的顺序读，然后自己改数值、删一行看报错——那是成本最低的理解方式。
