# 从零手写一个 AI Agent（V0 → V4）

一个**不用任何 agent 框架**、只用 `requests` 手写 HTTP 调用的小型 AI agent 学习项目。

目标不是尽快做出成品，而是通过**逐版加一个能力**，把 agent 的运行机制拆开看清楚：模型到底做了什么、你的代码做了什么、工具是怎么被调用的、记忆存在哪、权限边界在哪。

> 适合：会一门编程语言（尤其是 Java）、想弄懂 AI agent 内部怎么转的人。
> 不适合：想直接找一个生产级框架的人——这里刻意不引入 LangChain / CrewAI / 官方 SDK。

---

## 它现在能做什么

到 V4 为止，它是一个**能聊天、能记事、能整理文件、能自己安排步骤**的命令行助手：

- 多轮对话，记得你之前说过什么（`/clear` 可清空记忆）
- 自己决定要不要调用工具、调哪个、传什么参数
- 能列目录、读文件、建目录、移动文件、按模式搜文件、写文本文件
- 处理任务时先勘察、再说计划、再执行、最后复查
- **只能操作 `workspace/` 目录**，越界直接拒绝
- **写操作前必须人工确认**（输入 `y` 才执行）
- 每次会话写 JSONL 日志（`logs/`），并按 token 计预算（默认 20000）

---

## 版本路线

| 版本 | 文件 | 新增能力 | 要讲清的核心概念 |
|---|---|---|---|
| **V0** | `src/v0_chat.py` | 一次问答 | HTTP 请求结构、`messages` 数组、`system`、token 用量、为什么模型没有记忆 |
| **V1** | `src/v1_tool.py` | 单工具调用 | JSON Schema、`tools` 定义、`stop_reason=tool_use`、`tool_result` 回填 |
| **V2** | `src/v2_repl.py` | 多轮对话 + 记忆 | 历史累积与全量重发、`/clear`、上下文成本、错误重试 |
| **V3** | `src/v3_files.py` | 文件整理 | 沙箱与路径校验、写操作确认、dry-run、按职责拆包 |
| **V4** | `src/v4_assistant.py` | 多工具 + 简单规划 + 日志 + 成本控制 | 工具选择策略、system prompt 如何驱动规划、JSONL 可观测性、token 预算 |

每一版的讲义在 [`docs/notes/`](docs/notes/)：

- [V0：一次问答](docs/notes/v0.md)
- [V1：模型自己决定调工具](docs/notes/v1.md)
- [V2：多轮对话与记忆](docs/notes/v2.md)
- [V3：文件整理、沙箱与「人把关」](docs/notes/v3.md)
- [V4：多工具、任务规划、日志与成本控制](docs/notes/v4.md)

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

# 可选：给 V4 换一个更小的预算，演示「预算用完就停」
$env:MAX_SESSION_TOKENS = "800"
```

V2 / V3 / V4 是交互式 REPL：

```
你> 把 workspace 里的图片都移动到 images 目录
助手> （先列计划，问你是否确认）
你> y
[计划] 创建目录 workspace/images
执行? (y/N) y
...
/clear   # V2/V3 清空记忆；V4 同时重置预算
/exit    # 退出
```

V4 启动时还会打印本次日志路径，例如 `logs/session-20261110-115235.log`。

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
  agent/              # V3 起抽出的公共包
    client.py         #   和模型通信（发请求、重试、解析）
    tools.py          #   V3 的 4 个工具（V4 不改它，保留快照）
    sandbox.py        #   路径校验（只能操作 workspace）
    tools_v4.py       #   V4 的 6 个工具 + 日志摘要
    logger.py         #   JSONL 会话日志
    budget.py         #   token 预算

workspace/            # agent 唯一被允许改动的目录
logs/                 # V4 的会话日志（已 gitignore）
docs/
  notes/              # v0.md ~ v4.md：每版讲义
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

### 2. 写操作确认

`mkdir` / `move_file` / `write_file` 默认只打印计划，用户输入 `y` 才执行：

```python
WRITE_TOOLS = WRITE_TOOLS_V3 | {"write_file"}

if name in WRITE_TOOLS:
    print(f"[计划] {describe_write_v4(name, args)}")
    if input("执行? (y/N) ").strip().lower() != "y":
        return "用户拒绝了这次操作，未执行。"
```

`write_file` 还会显示内容预览（前 40 字符），并且目标已存在时必须显式传 `overwrite=true`，否则拒绝覆盖。

### 3. 不提供 shell

本阶段**不提供** `run_shell` 工具。沙箱的保护范围 = 你提供的工具集合；一旦能执行任意命令，应用层路径检查就不够了，需要升级到容器/虚拟机级别的隔离。

### 4. Key 不进文件

API Key 只从 `ARK_API_KEY` 环境变量读取，`.env` 已被 `.gitignore` 忽略。

> 如果这个 key 曾经出现在任何提交或公开位置，请立即去火山方舟控制台作废并重新生成。

### 5. 日志不泄密

V4 的日志写到 `logs/`（已 gitignore）。`user_message` 只记字符数、不记原文；`write_file` 的参数只记 `path / chars / overwrite`，不记全文。日志用于事后查「做了什么、花了多少」，不是用来存档聊天内容。

---

## 设计原则

1. **不用框架**：agent 的骨架很小——「问模型 → 执行工具 → 结果回喂 → 再问」，自己写一遍才能真正理解。
2. **一次只加一个概念**：每版都能独立跑通，且能说清「这一版新增的 X 行解决了什么问题」。
3. **抽象等需求逼出来**：V1/V2 刻意不拆包，V3 才拆。
4. **错误信息是给模型看的**：工具报错返回可读文字，而不是让程序崩——模型据此自我纠正。
5. **危险操作要有人把关**：模型可以提议，但不能单方面执行不可逆操作。

---

## 说明

这是一个**个人学习项目**，不是生产级工具。它的价值在于「每一行都能解释为什么」，而不在于功能完整或性能。

如果你也想走一遍这条路，建议按 `docs/notes/` 的顺序读，然后自己改数值、删一行看报错——那是成本最低的理解方式。
