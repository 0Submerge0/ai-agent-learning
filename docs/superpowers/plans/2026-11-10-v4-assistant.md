# V4 多工具 + 简单规划 + 日志 + 成本控制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 V3 的「4 个工具的文件助手」升级成「6 个工具、会先勘察再规划、每一步可追溯、花费有上限」的收口版。

**Architecture:** 不改 V3 的 `agent/tools.py`（保留 V3 快照），新增三个模块：
- `agent/logger.py`：把每个事件写成一行 JSON（JSONL），落到 `logs/`。
- `agent/budget.py`：按 token 计的会话预算，超限即停。
- `agent/tools_v4.py`：复用 V3 的 4 个工具函数，追加 `search_files`、`write_file`，并提供带日志的分发器。
新入口 `src/v4_assistant.py`。**不引入显式 plan 数据结构**——规划靠 system prompt 驱动（符合设计文档 5.8）。

**Tech Stack:** Python 3.14（uv）、`requests`、标准库 `json`/`time`/`pathlib`。

**Spec:** `docs/superpowers/specs/2026-10-29-ark-agent-design.md`

## Global Constraints

- 沙箱与写操作确认**原样保留**：所有路径过 `safe_path`，所有写工具先打印计划、`y` 才执行
- 不提供 `run_shell`、不提供删除
- 依赖仍只有 `requests`
- 日志**不得记录 API key**；只记事件、工具名、参数、token 数
- 预算默认 20000 tokens，可用环境变量 `MAX_SESSION_TOKENS` 覆盖
- V3 的 `src/v3_files.py` 与 `agent/tools.py` **一行不改**，保证 V3 仍能原样跑
- `MAX_ITERATIONS = 10`（多步整理比 V3 的 8 更需要余量）

## Review Focus

1. **日志可读且不泄密**：每个事件一行 JSON，含时间戳；永远不写 key。
2. **预算超限立即停**：超过上限后不再执行工具、不再发起新的模型调用。
3. **新工具仍受沙箱约束**：`search_files` 的模式不能含 `..`，结果必须落在 workspace 内。
4. **`write_file` 先预览再确认**：计划里要显示内容前几十个字符，避免「盲签」。
5. **V3 不被破坏**：`uv run python src/v3_files.py` 仍按 V3 的行为工作。

---

### Task 1: 日志模块

**Files:**
- Create: `src/agent/logger.py`

- [ ] **Step 1: 写 `SessionLogger`**

构造时创建 `logs/session-YYYYMMDD-HHMMSS.log`，`log(event, **fields)` 写入一行 JSON 并 `flush()`，`close()` 关闭文件。`LOG_DIR` 用 `Path(__file__)` 相对定位。

- [ ] **Step 2: 本地验证**

Run: 用 `uv run python -c` 创建一个 logger，写入两条事件，读回文件确认是两行合法 JSON。

Expected: 两行 JSON，含 `ts` 与 `event` 字段。

---

### Task 2: 预算模块

**Files:**
- Create: `src/agent/budget.py`

- [ ] **Step 1: 写 `Budget`**

`Budget(max_tokens)` 维护 `input_tokens` / `output_tokens`；`add(usage)` 累加并返回 `"ok" | "warning" | "exhausted"`（80% 警告、100% 耗尽，警告只报一次）；`reset()` 清零。

- [ ] **Step 2: 本地验证**

Run: 用 `uv run python -c` 连续 `add` 若干次，确认状态从 ok → warning → exhausted，且 warning 只返回一次。

---

### Task 3: V4 工具集

**Files:**
- Create: `src/agent/tools_v4.py`

**Interfaces:**
- Consumes: `agent.tools` 的 4 个函数与 `describe_write`、`agent.sandbox` 的 `safe_path`/`rel`/`WORKSPACE`
- Produces: `TOOLS_V4`、`WRITE_TOOLS`、`run_tool(name, args, ask=None, log=None)`、`search_files`、`write_file`

- [ ] **Step 1: 写 `search_files(pattern, path=".")`**

只读。`safe_path(path, must_exist=True)` 定位基准目录；模式拒绝绝对路径与 `..`；`rglob` 结果逐个 `resolve()` 并检查 `is_relative_to(WORKSPACE)`；最多 50 条；返回相对路径与大小。

- [ ] **Step 2: 写 `write_file(path, content, overwrite=False)`**

写操作。`safe_path(path)` 校验；目标已存在且 `overwrite=False` 时拒绝；自动建父目录；UTF-8 写入；返回「已写入 …（N 字符）」。

- [ ] **Step 3: 写 `TOOLS_V4` 与 `run_tool`**

`TOOLS_V4 = TOOLS (V3) + [search_files, write_file]`；`WRITE_TOOLS = WRITE_TOOLS_V3 | {"write_file"}`。`run_tool` 自己维护 6 个工具的 registry；写工具先 `print("[计划] …")` 再问 `y/N`；`log` 是可选回调，用于记录 `write_plan` / `write_confirm`；异常一律转文字。

- [ ] **Step 4: 本地验证（不调模型）**

Run: 测 `search_files("*.jpg")`、`write_file` 拒绝覆盖、`write_file` 越界、`ask="n"` 时 `write_file` 不生效。

Expected: 四条都符合预期，程序不抛未捕获异常。

---

### Task 4: V4 入口

**Files:**
- Create: `src/v4_assistant.py`

- [ ] **Step 1: 写 `run_turn(messages, budget, logger)`**

与 V3 相同的工具循环，额外做四件事：
1. 每次调用模型前记 `model_request`，调用后记 `model_response`（含 tokens、耗时、stop_reason）。
2. `budget.add(usage)`，`exhausted` 时打印并 `return`（**不执行工具**）。
3. 工具调用前后记 `tool_call` / `tool_result`。
4. 每轮结束打印会话累计用量。

- [ ] **Step 2: 写 `run_repl()`**

保留 `/clear`（现在同时重置记忆与预算）、`/exit`、`ModelError` 捕获、UTF-8 修复；启动时打印日志路径与预算；`finally` 里写 `session_end` 并关日志。

- [ ] **Step 3: 正常路径验证（多步规划）**

在 `workspace/` 造混合文件（图片、文本、其他），问：「帮我整理 workspace：图片放 images/，文本放 texts/，其他放 other/，最后生成 report.md 记录你做了什么。」

Expected: 模型先勘察、再说计划、逐个执行（每次写操作要 `y`）、最后复查并写报告；日志文件里有完整事件序列。

- [ ] **Step 4: 预算演示**

用 `MAX_SESSION_TOKENS=800` 跑一个任务。

Expected: 达到上限后打印「预算用完」，停止调用，不执行剩余工具；输入 `/clear` 可重置。

- [ ] **Step 5: 提交**

```bash
git add src/agent/logger.py src/agent/budget.py src/agent/tools_v4.py src/v4_assistant.py
git commit -m "feat(v4): 多工具 + 简单规划 + 日志 + 成本控制"
```

---

### Task 5: V4 讲义 + README 更新

**Files:**
- Create: `docs/notes/v4.md`
- Modify: `README.md`（V4 状态、运行方式）

- [ ] **Step 1: 写讲义**

三件事：
1. **规划不是数据结构，是提示词行为**——同一套循环，靠 system prompt 让它先勘察、再计划、再执行、再复查。
2. **日志是「事后能解释」的前提**——模型行为不可复现，日志把「它做了什么、花了多少」变成可查的事实。
3. **成本控制要设在「花钱之前」**——预算检查放在执行工具之前，超限就不动手。

- [ ] **Step 2: 逐条重跑命令，核对行号与输出**

- [ ] **Step 3: 提交**

```bash
git add docs/notes/v4.md README.md
git commit -m "docs(v4): V4 讲义与 README 更新"
```
