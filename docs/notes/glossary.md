# V0–V6 全量知识清单

> 覆盖 `src/` 下 16 个源文件里出现的**所有概念、名词、技术、API、语法特性**。
> 每条都标了「落在哪个文件」，可以一边读一边对照。
> 末尾有「逐版新增对照表」和「面试高频 30 词」。

---

## 怎么用这份清单

- **第一遍读代码时**：只看每层的分类标题，建立「这块属于哪一类」的感觉。
- **遇到陌生名词时**：来查一句话定义，再跳到对应文件看真身。
- **面试前**：只背最后一节「高频 30 词」，那是真正会被问的。

标了 ⚠️ 的，是本项目**故意没做**、但面试可能问到的。

---

# 全景：八层知识栈

| 层 | 主题 | 对应文件 |
|---|---|---|
| L1 | 网络与 HTTP 协议 | `v0_chat.py` / `client.py` |
| L2 | LLM 基础概念 | 全部 |
| L3 | 工具调用机制 | `v1_tool.py` / `tools*.py` |
| L4 | Agent 架构与状态 | `v5_planner.py` / `v6_control.py` / `plan.py` |
| L5 | Python 语言特性 | 全部 |
| L6 | 文件、路径与安全 | `sandbox.py` / `tools.py` |
| L7 | 可观测性与成本 | `logger.py` / `budget.py` |
| L8 | 工程与设计原则 | 全部 |

---

# L1 网络与 HTTP 协议

| 名词 | 一句话 | 落在哪 |
|---|---|---|
| **HTTP POST** | 客户端把数据放进请求体发给服务端 | `v0_chat.py` `chat()` |
| **Endpoint（端点）** | 一个 URL 代表一个服务入口 | `API_URL` |
| **Anthropic Messages API** | 一套对话协议：`system` 在顶层、`messages` 里只有 user/assistant | `client.py` |
| **OpenAI Chat Completions** | 另一套协议：system 也是 messages 里的一条 role | ⚠️ 未用，但最常见 |
| **请求头 header** | 随请求发送的元信息 | `client.py` |
| **`x-api-key`** | Anthropic 系的鉴权头（不是 `Authorization: Bearer`） | `client.py` |
| **`anthropic-version`** | 协议版本号，决定字段含义 | `2023-06-01` |
| **`content-type`** | 声明请求体是 JSON | `application/json` |
| **请求体 body / payload** | 真正送给模型的内容 | `client.py` 的 `payload` |
| **JSON 序列化** | Python dict → JSON 字符串 | `requests` 的 `json=` 参数自动做 |
| **JSON 反序列化** | JSON 字符串 → Python dict | `resp.json()` |
| **状态码 200** | 成功 | `if resp.status_code != 200` |
| **状态码 400** | 请求参数错（最常见：字段缺失/类型错） | 同上 |
| **状态码 401** | key 无效 / 失效 | 同上 |
| **状态码 429** | 限流（请求太频繁） | 同上 |
| **状态码 5xx** | 服务端故障 | 同上 |
| **`raise_for_status()`** | requests 的便捷方法，会抛异常 | **故意不用**：要读 `resp.text` 里的 `error.code` |
| **timeout（超时）** | 超过 N 秒就放弃，防止永久挂起 | `timeout=60` |
| **`requests.exceptions.Timeout`** | 连上了但没按时返回 | 可重试 |
| **`ConnectionError`** | 根本没连上 | 可重试 |
| **`RequestException`** | 所有 requests 异常的基类 | 不重试（请求构造有问题） |
| **重试 retry** | 失败后自动再试 N 次 | `retries=2`（最多 3 次） |
| **为什么 401 不重试** | 重试不会让错误 key 变对，只会多花钱 | `client.py` 注释 |
| **指数退避 exponential backoff** | 每次重试等更久（1s→2s→4s） | ⚠️ 未做，只立即重试 |
| **代理 / 网关拦截页** | 返回 200 但内容是 HTML，不是 JSON | `except ValueError` 分支 |
| **SSE（Server-Sent Events）** | 流式输出的传输格式，逐块推送 | ⚠️ 未做（选项 C） |
| **编码 UTF-8** | 中文正确的字节表示 | `reconfigure(encoding="utf-8")` |
| **GBK / cp936** | Windows 中文默认编码，会吃掉 UTF-8 | `sys.stdin.reconfigure` 就是为它 |
| **`errors="replace"`** | 解码失败时用替换符代替崩溃 | `read_text` / `reconfigure` |
| **环境变量** | 进程外注入的配置，不落代码 | `os.environ["ARK_API_KEY"]` |
| **密钥打码** | 报错里把 key 替换成 `***` | `str(exc).replace(api_key, "***")` |

---

# L2 LLM 基础概念

| 名词 | 一句话 | 落在哪 |
|---|---|---|
| **token** | 模型的最小计费单位，约等于 1~2 个汉字 | `usage` 字段 |
| **input_tokens** | 你这次发过去的量（含 system + 全部历史 + 工具定义） | `usage.get("input_tokens")` |
| **output_tokens** | 模型这次生成的量 | `usage.get("output_tokens")` |
| **计费方向** | 输入输出都算钱；多数模型输出单价更高 | `budget.py` 分开记两个数 |
| **context window（上下文窗口）** | 一次请求能容纳的最大 token 数 | ⚠️ 未显式检查，靠预算兜 |
| **`max_tokens`（输出上限）** | 模型这次最多生成多少 | `client.py` `MAX_TOKENS = 1024` |
| **两个 max_tokens 同名不同义** | 输出上限 vs 会话总预算 | `budget.py` 文件头专门讲了 |
| **system prompt** | 每次请求附带的「岗位说明书」，不进入对话历史 | `client.py:61` |
| **user message** | 用户说的话 | `messages` |
| **assistant message** | 模型说过的话（要原样回填） | `messages.append` |
| **role** | 消息的说话人 | `user` / `assistant` |
| **服务端无状态 stateless** | 模型不保存任何会话；「记忆」全在本地 `messages` 里 | `v2_repl.py` |
| **上下文累积** | 每轮都把完整历史重发一遍，所以越聊越贵 | `run_turn` 的 `messages` |
| **`stop_reason`** | 模型为什么停下，是循环的开关 | `end_turn` / `tool_use` / `max_tokens` |
| **`end_turn`** | 模型正常说完了 | 循环出口 |
| **`tool_use`** | 这一批回复里有工具调用 | 循环继续的信号 |
| **`max_tokens`（stop_reason 值）** | 被输出上限截断了 | `v1_tool.py` 会提示 |
| **`stop_sequence`** | 命中自定义停止词 | ⚠️ 协议有，未用 |
| **content blocks（内容块数组）** | 回复不是一个字符串，是一组带 `type` 的块 | `data["content"]` |
| **text block** | `{"type":"text","text":"..."}` | `extract_text()` |
| **thinking block** | 模型的思考过程，`{"type":"thinking","thinking":"..."}` | 实测本模型不带 `signature` |
| **signature（思考签名）** | 部分模型要求把思考块原样带回并验签 | ⚠️ 本模型没带，换模型要小心 |
| **为什么原样回填 assistant** | 官方要求；（本模型丢了也能跑，但别赌） | `v1_tool.py:203` 注释 |
| **多模态 multimodal** | 一条消息里混图片/文字块 | ⚠️ 未用 |
| **temperature** | 采样随机性，0 最确定 | ⚠️ 未设，用默认 |
| **top_p** | 核采样阈值 | ⚠️ 未用 |
| **幻觉 hallucination** | 模型凭空编造事实 | 提示词「不要凭空猜测」就是防它 |
| **提示词工程 prompt engineering** | 用文字约束模型行为 | 每个文件的 `SYSTEM_PROMPT` |
| **静态 + 动态分层** | 不变的部分（身份）+ 每轮变的部分（状态） | V5 把 `plan.render()` 塞进 system |
| **每轮重渲染** | 动态部分每轮重新生成，模型总看到最新状态 | `execution_prompt()` |
| **复述（Manus 经验）** | 反复重写目标，把注意力拉回近期 | 等价于每轮贴计划 |
| **prompt caching / KV cache** | 前缀不变可命中缓存，省钱又快 | ⚠️ 未用（Manus 那条取舍） |
| **上下文工程 context engineering** | 决定「哪些信息进上下文、以什么形式、多久」 | 整个 V5 的设计 |
| **few-shot** | 在提示词里给几个例子 | ⚠️ 未用 |
| **chain-of-thought（CoT）** | 让模型先推理再回答 | thinking 块就是它的一种实现 |
| **structured output / JSON mode** | 强制模型输出合法 JSON | ⚠️ 未用；我们用工具 schema 间接约束 |

---

# L3 工具调用机制

| 名词 | 一句话 | 落在哪 |
|---|---|---|
| **tool use / function calling** | 模型输出「我想调哪个函数、什么参数」的数据 | `tool_use` 块 |
| **`tools` 字段** | 请求体里告诉模型「有哪些工具可用」 | `payload["tools"]` |
| **工具说明书** | `name` + `description` + `input_schema` | `TOOLS` 列表 |
| **`input_schema`** | JSON Schema 子集，描述参数结构 | `TOOLS` |
| **`required`** | 哪些参数必填 | `"required": ["path"]` |
| **`enum`** | 参数只能取枚举里某个值 | `UPDATE_TASK_TOOL` 的 `status` |
| **`description` 决定选中率** | 模型看不到实现，只能靠描述判断该不该用 | 每个工具的 `description` |
| **`tool_use` 块** | `{"type":"tool_use","id":"call_..","name":"..","input":{..}}` | 模型输出 |
| **`tool_use_id`** | 一次工具调用的唯一标识，用于配对结果 | `block["id"]` |
| **`tool_result` 块** | `{"type":"tool_result","tool_use_id":"..","content":".."}` | 我们构造 |
| **配对规则** | 每个 `tool_use` 必须有且只有一条对应 `tool_result` | 缺了就 400 |
| **为什么 tool_result 是 user 角色** | 协议只有 user/assistant 两种；工具结果不是模型说的 | `v1_tool.py:221` 注释 |
| **一条 user 消息装多个 result** | 一批工具调用的结果合并成一条消息 | `results` 列表 |
| **并行工具调用** | 一次回复里发多个 `tool_use` 块 | 实测一次发过 4 个 |
| **registry（分发表）** | 名字 → 函数的字典，唯一定义「模型能干什么」 | `tools.py:177` |
| **白名单机制** | 不在 registry 里的名字，调不到任何东西 | `if fn is None: return` |
| **`fn(**args)`** | 把 dict 按名字展开成关键字参数 | `tools.py:202` |
| **`**kwargs`** | 收集任意关键字参数 | `logger.log(self, event, **fields)` |
| **工具循环 agent loop** | 问模型 → 执行 → 回填 → 再问 | `run_agent()` |
| **`MAX_ITERATIONS`** | 防「无限调工具不收敛」 | V1=5, V3=8, V4=16 |
| **错误即数据 error as data** | 工具异常转成字符串喂回去，程序不崩 | `except Exception as exc: return ...` |
| **自我纠错** | 模型读到「路径不存在」会自己换参数 | 依赖上一条 |
| **字符串前缀判断（脆弱）** | 用 `startswith("工具执行失败")` 猜状态 | `log_tool_result`，注释里标了「脆弱」 |
| **禁用工具 = 不给 schema** | 阶段隔离的物理实现 | V5 的两套清单 |
| **工具数量与缓存冲突** | 换工具清单会让 KV 缓存失效 | ⚠️ V5 vs Manus 的取舍 |
| **`__all__`** | 声明模块对外公开的名字 | `tools_v5.py` 末尾 |

---

# L4 Agent 架构与状态

| 名词 | 一句话 | 落在哪 |
|---|---|---|
| **Agent 的定义** | 一个 while 循环：问模型 → 执行工具 → 回填 → 再问 | `run_agent()` |
| **用户驱动 vs 计划驱动** | V1–V4 一问一动；V5+ 一次授权连续推进 | `execution_prompt` 规则 4 |
| **显式计划 explicit plan** | 把「计划」从一句话变成可检查的对象 | `plan.py` |
| **`dataclass`** | 自动生成 `__init__`/`__repr__`/`__eq__` 的类 | `Task` / `Plan` |
| **`field(default_factory=list)`** | 每个实例独立的新列表，避开可变默认值陷阱 | `Plan.tasks` |
| **可变默认值陷阱** | `tasks: list = []` 会让所有实例共享同一个列表 | `plan.py` 注释 |
| **状态机 state machine** | 5 个状态 + 合法的迁移规则 | `STATUSES` |
| **`pending`** | 还没做 | — |
| **`in_progress`** | 正在做（**最危险**：中断后模型会以为还在做） | V5 恢复的坑 |
| **`done`** | 做完了 | — |
| **`failed`** | 试过，没成 | 不算完成 |
| **`skipped`** | 不需要做（人工跳过） | **算完成** |
| **`is_complete`** | 全部 done/skipped；`failed` 不算 | `plan.py:162` |
| **「完成」是谁算的** | 代码算，不是模型宣布 | 这是 V5 的质变 |
| **进度派生值** | `done_count` / `progress_text()` / `counts()` / `summary()` 都是算出来的，不存储 | `plan.py` |
| **`@property`** | 让方法能当字段用，值永远是最新的 | `budget.used` / `plan.is_complete` |
| **`render()`** | 把计划拍成文本，给人也给模型看 | `plan.py:175` |
| **两道闸门** | ①批准计划 ②确认每次写操作 | `approve_plan` + `WRITE_TOOLS` |
| **为什么不能合成一道** | 一个管「方向对不对」，一个管「这一步要不要做」 | 面试点 |
| **人工接管 human-in-the-loop** | 人能改计划：`/skip /retry /edit /add /rm` | `v6_control.py` |
| **为什么改计划不给模型** | 计划是用户批准的，模型能改就等于闸门漏了 | `v6.md §2.2` |
| **状态工具** | `create_plan` / `update_task` 改的是对象，不是文件 | `tools_v5.py` |
| **阶段隔离 phase isolation** | 规划只能读，执行才能写 | `PLANNING_TOOLS` / `EXECUTION_TOOLS` |
| **结构上不可能 vs 提示词劝阻** | 请求里没有这个函数，比「请不要用」可靠 | V5 核心设计 |
| **防御第二层** | 即使模型硬调，`run_*_tool` 也拒绝 | `run_planning_tool` 兜底 |
| **断点恢复 checkpoint / resume** | 现场落盘，下次读回来接着跑 | `session_store.py` |
| **`phase` 字段** | 停在哪一步：`planning`/`awaiting_approval`/`executing`/`done`/`rejected` | `session_store.py` |
| **`adopt()`** | 恢复后保存写回**同一个**文件，不新建 | `session_store.py:58` |
| **`latest()` 按 mtime** | 不按文件名：adopt 覆写后文件名的时间会旧 | `session_store.py:107` |
| **序列化 / 反序列化** | 对象 ↔ JSON dict | `to_dict` / `from_dict` |
| **`@classmethod` 工厂方法** | 第一个参数是 `cls`，返回新实例 | `Plan.from_dict` |
| **`restore()` 取较大的上限** | 允许你调大预算继续跑，不允许偷偷调小 | `budget.py:106` |
| **预算保险丝 budget** | 超限就停，防止一次任务烧掉整月额度 | `budget.py` |
| **检查点稀疏** | 只在每轮开头检查，一轮就可能冲爆 | V4/V5 的洞 |
| **软着陆 soft landing** | 剩余不够下一轮就提前收尾，别硬停 | V6 `execution_phase` 出口 2 |
| **轮次缩放** | 上限 = `max(16, 3*剩余 + 4)` | `iterations_for()` |
| **出口（exit）分析** | `execution_phase` 有 5 个 `return`，只有 1 个能返回 `done` | 文件头有表 |
| **过期事实 stale fact** | 历史里那条「预算已用完」现在不成立了 | V6 核心发现 |
| **状态与对话不一致** | `plan`/`budget` 在内存里，模型只能看见 `messages` | `takeover_message` |
| **接管消息** | 人工干预后补一条 user 消息，把新事实告诉模型 | `v6_control.py` |
| **为什么用 user 不用 system** | system 每轮都发，写进去会变成永久指令 | 一致性 |
| **`edits` 累积** | 记下这次接管改了什么，`/go` 时一起说 | `v6_control.py` |
| **phase 从 done 拉回 executing** | 加了任务就又是「没做完」，否则 `/resume` 会说已完成 | `v6_control.py` |
| **ReAct** | Reason + Act 的经典范式（思考→行动→观察） | 我们的循环是它的简化版 |
| **plan-and-execute** | 先出计划再执行，V5 就是这一派 | — |
| **reflection / 反思** | 执行后自我检查错在哪 | ⚠️ 未做 |

---

# L5 Python 语言特性

> 环境：Python **3.14.4**，依赖只有 `requests 2.34.2`。

## 类型标注

| 特性 | 说明 | 落在哪 |
|---|---|---|
| `list[str]` | 3.9+ 的内置泛型写法 | `set_tasks(titles: list[str])` |
| `dict[str, int]` | 同上 | `Plan.counts()` |
| `int \| None` | 3.10+ 的联合类型 | `max_iterations: int \| None` |
| `tuple[str, bool]` | 返回多值 | `run_planning_tool` |
| `-> None` | 明确返回空 | 各 `run_repl()` |
| 标注不强制 | 运行时不做检查，只是文档 + IDE 提示 | 模型给的 `"3"` 照样能进 |

## 数据与容器

| 特性 | 说明 | 落在哪 |
|---|---|---|
| `@dataclass` | 自动生成构造/打印/比较 | `Task` / `Plan` |
| `field(default_factory=...)` | 每实例独立默认值 | `Plan.tasks` |
| 列表推导 | `[Task(...) for i, t in enumerate(clean)]` | `set_tasks` |
| 生成器表达式 | `sum(1 for t in tasks if ...)` | `done_count` |
| `all(...)` / `any(...)` | 全真 / 有真 | `is_complete` |
| `next((x for ...), None)` | 找第一个，找不到给默认 | `Plan.find` |
| `max((...), default=0)` | 空序列也不炸 | `Plan.add` 的 `new_id` |
| `enumerate(seq, start=1)` | 同时拿序号和元素 | 重新编号 |
| `sorted(key=lambda)` | 稳定排序 | `list_dir` |
| `dict.get(k, 默认值)` | 缺键不抛异常 | 到处都是 |
| `set \| set` | 集合并集 | `WRITE_TOOLS = ... \| {"write_file"}` |
| `in` 判断集合 | O(1) 查成员 | `if name in WRITE_TOOLS` |
| `str.startswith` | 前缀判断 | `log_tool_result`（脆弱点） |
| `str.strip()` / `.lower()` | 去空白 / 转小写 | 确认闸门的 `!= "y"` |
| `str.split(maxsplit=2)` | 最多切几刀 | `/edit 3 新标题` |
| `str.removeprefix` | 3.9+，去前缀 | `session_id` 还原 |
| `!r` 格式化 | 带引号打印原值 | `/skip abc` 的错误提示 |
| `len()` 当「进度」用 | 字符数/条数 | 日志里的 `chars` |

## 函数与参数

| 特性 | 说明 | 落在哪 |
|---|---|---|
| 默认参数 | `path: str = "."` | `list_dir` |
| 关键字-only 参数 | `def save(self, *, goal, phase, ...)`：调用必须写名字 | `session_store.py:69` |
| `ask=None` 依赖注入 | 测试时传假函数，不用真的 `input()` | `run_tool` |
| `log=None` 钩子 | 有日志就记，没有也能跑 | 各 `run_tool` |
| `**args` 展开 | `fn(**{"path": "."})` == `fn(path=".")` | `tools.py:202` |
| `**fields` 收集 | `log("event", a=1)` 里 `a=1` 全收进 dict | `SessionLogger.log` |
| 闭包/回调 | 把函数当参数传 | `logger.log` 传给 `run_turn` |

## 异常与控制流

| 特性 | 说明 | 落在哪 |
|---|---|---|
| `try/except/else/finally` | `finally` 保证收尾一定执行 | `run_repl` |
| 自定义异常 | `class ModelError(Exception)` | `client.py` |
| 只捕特定异常 | 只重试 Timeout/ConnectionError | 不重试 401 |
| `except Exception as exc` | 兜底，把异常变文字 | `run_tool` |
| `raise ... from` | ⚠️ 未用，可保留原始异常链 | — |
| `EOFError` | `input()` 读到管道结束 | 当「拒绝」处理 |
| `KeyboardInterrupt` | Ctrl+C | 正常退出，不打印堆栈 |
| `for ... else` | ⚠️ 未用 | — |
| 早返回 early return | `if fn is None: return ...` 省掉一级缩进 | 常见风格 |
| `continue` 驱动循环 | 工具批做完回循环开头 | `execution_phase` |
| `break` 退出 REPL | `/exit` | — |

## 上下文管理器

| 特性 | 说明 | 落在哪 |
|---|---|---|
| `with` 语句 | 离开块自动清理 | ⚠️ 当前用 try/finally |
| `__enter__` / `__exit__` | 自定义 with 行为 | `logger.py` 备用 |
| `contextlib.redirect_stdout` | 临时改 stdout，用来抓打印 | 我做实验时用过 |

## 模块与入口

| 特性 | 说明 | 落在哪 |
|---|---|---|
| `if __name__ == "__main__"` | 直接运行才执行，被 import 时不执行 | 每个 vN 文件 |
| `sys.argv` | 命令行参数 | `" ".join(sys.argv[1:])` |
| `sys.exit(1)` | 带退出码结束进程 | V0 的错误分支 |
| `sys.stderr` | 错误输出流 | 所有错误提示 |
| `sys.stdout.reconfigure` | 运行中改编码 | 每个入口 |
| `hasattr` | 判断对象有没有某属性 | 兼容被重定向的流 |
| `from .x import y` | 相对导入（同包内） | `agent/*.py` |
| `from x import y as z` | 别名，避免同名冲突 | `run_tool as run_v4_tool` |
| `sys.path.insert(0, "src")` | 让脚本能 import 项目模块 | 我的临时脚本 |
| `import` 即执行 | 模块顶层代码在首次 import 时跑 | `WORKSPACE` 常量 |

## 标准库

| 模块 | 用到的能力 | 落在哪 |
|---|---|---|
| `pathlib.Path` | 面向对象的路径操作 | 到处 |
| `json` | `dumps` / `loads` | 日志、存档 |
| **`os`** | `os.environ` 读环境变量 | 所有读 key 的地方 |
| **`sys`** | argv/exit/stdout/stderr | 入口 |
| **`time`** | `time.time()` 记耗时 | `elapsed_ms` |
| **`datetime`** | `now().isoformat(timespec=...)` | 时间戳 |
| **`dataclasses`** | `dataclass` / `field` | `plan.py` |
| **`subprocess`** | ⚠️ 只在我做实验时用过，项目本身不用 | — |

---

# L6 文件、路径与安全

| 名词 | 一句话 | 落在哪 |
|---|---|---|
| **沙箱 sandbox** | 把 agent 的能力限制在一个目录里 | `sandbox.py` |
| **`WORKSPACE`** | 唯一允许操作的根目录 | 项目根下的 `workspace/` |
| **`__file__` 定位** | 用源码位置推算项目根，不写死盘符 | `.parent.parent.parent` |
| **`resolve()`** | 展开 `..` 和符号链接，得到真实路径 | `sandbox.py:51` |
| **`is_relative_to()`** | 按**路径段**判断是否子目录（3.9+） | `sandbox.py:56` |
| **为什么不能 `startswith`** | `workspace_evil` 会被误判成 `workspace` 的子目录 | 注释里讲了 |
| **顺序不能反** | 先 resolve 再比较，否则符号链接能骗过检查 | 核心考点 |
| **路径穿越 path traversal** | `../../Windows/System32` 逃出沙箱 | 被 resolve + 前缀检查拦住 |
| **符号链接 symlink 攻击** | `workspace/link -> C:\Windows` | resolve 后暴露真实位置 |
| **`rglob` 也要再验** | 它跟符号链接会跑出沙箱 | `search_files` 逐个 `is_relative_to` |
| **`must_exist`** | 读操作为 True；创建类为 False | `safe_path` 参数 |
| **`rel()`** | 绝对路径 → 相对 workspace 显示 | 省 token、好读 |
| **`as_posix()`** | 反斜杠统一成正斜杠 | 避免 `images\\a.jpg` 转义 |
| **`expanduser()`** | `~` → 用户目录 | V1 的 `list_dir` |
| **`iterdir()` / `stat()`** | 遍历子项 / 取文件大小 | `list_dir` |
| **`mkdir(parents=True, exist_ok=True)`** | 一次建多层、并发不报错 | `mkdir` |
| **`rename()`** | 同分区内原子移动 | `move_file` |
| **`replace()`** | 覆盖式原子替换 | 存档 |
| **`read_text(encoding="utf-8")`** | 一次性读文本 | `read_file` |
| **`write_text(...)`** | 一次性写文本 | `write_file` |
| **`parent.mkdir`** | 写文件前先建父目录 | `write_file` |
| **截断 truncate** | 只取前 N 条/前 N 字符 | `read_file` 4000 字符 |
| **必须告诉模型「我截断了」** | 否则它以为内容就这么少 | 两处注释 |
| **拒绝覆盖** | 目标已存在就报错，不默认覆盖 | `move_file` 无条件拒绝 |
| **`overwrite` 显式参数** | 要覆盖必须写 `true`，会出现在预览里 | `write_file` |
| **原子写 atomic write** | 先写 `.tmp` 再 `replace` | `session_store.py:104` |
| **为什么不直接写目标** | 断电会留下半个 JSON，整个存档报废 | 核心考点 |
| **`Path.stem`** | 去扩展名的文件名 | `adopt` |
| **`glob` / `rglob`** | 按模式找文件（递归） | `search_files` |
| **通配符 `*`** | 任意文件名部分 | `*.jpg` |

## 安全设计原则（概念层）

| 原则 | 一句话 | 落在哪 |
|---|---|---|
| **最小权限** | 只给完成当前任务所需的最小能力 | 阶段隔离 |
| **默认拒绝 fail-closed** | 不确定就当成「不执行」 | `!= "y"` 都算拒绝 |
| **白名单优于黑名单** | 列出允许的，比列出禁止的更安全 | `registry` |
| **提示词 ≠ 强制** | 提示词是建议，代码才是约束 | `safe_path` |
| **两道闸门各管一层** | 方向 + 每一步动作 | V5/V6 |
| **确认前必须能看懂** | 看不见内容就不该让人签 | `describe_write_v4` 预览 |
| **避免让人白按一次** | 能不能执行要先检查再问 | V4 的 overwrite 前置检查 |
| **密钥不进代码/日志/存档** | 只从环境变量读 | 实测 0 次匹配 |
| **日志防「记太全」** | 只记长度不记内容 | `summarize_args` |
| **错误不杀进程** | 一次失败不该终结会话 | `ModelError` |

---

# L7 可观测性与成本

| 名词 | 一句话 | 落在哪 |
|---|---|---|
| **日志 logging** | 把发生过的事记下来 | `logger.py` |
| **JSONL** | 每行一个独立 JSON，崩了也只丢最后一行 | `*.log` |
| **为什么不用 JSON 数组** | 少个 `]` 整个文件就废了 | `logger.py` 注释 |
| **事件 event** | 日志的 `event` 字段，标识发生了什么 | 见下表 |
| **结构化日志** | 字段固定、机器可解析，不是自由文本 | `log("tool_call", tool=..., args=...)` |
| **`flush`** | 立刻落盘，别等缓冲区 | 每条都 flush |
| **flush 的代价** | 每条多一次磁盘 IO，但保住了现场 | 注释里的取舍 |
| **`ensure_ascii=False`** | 中文原样写，不转 `\uXXXX` | 日志可读 |
| **`default=str`** | 不可序列化对象兜底转字符串 | Path 之类 |
| **`indent=2`** | 存档带缩进，人能直接看 | 存档 |
| **`isoformat(timespec=...)`** | 统一时间格式 | `2026-11-11T14:18:24` |
| **耗时统计** | `time.time()` 前后相减 × 1000 | `elapsed_ms` |
| **会话日志 vs 会话存档** | 一个是流水，一个是快照 | 两个目录 |
| **事后复盘** | 用日志还原「为什么第 7 轮选错了」 | 日志的价值 |

## 本项目记录的事件类型

| event | 含义 |
|---|---|
| `session_start` / `session_end` | 会话起止 |
| `user_message` | 用户说了多少字 |
| `goal` | 设定了目标 |
| `model_request` | 发请求前（带 message_count） |
| `model_response` | 收到回复（stop_reason / 耗时 / token） |
| `plan_text` | 模型顺带说的那段计划文本 |
| `tool_call` | 要调什么工具、参数摘要 |
| `tool_result` | 工具返回（ok / 长度 / 前 300 字） |
| `write_plan` / `write_confirm` | 打算写什么 / 用户批没批 |
| `budget_warning` / `budget_exhausted` | 预算告警 / 耗尽 |
| `soft_landing` | 预算不够，提前收尾 |
| `plan_created` / `plan_approved` / `plan_rejected` | 计划三态 |
| `task_updated` | 任务状态变化 |
| `plan_edited` | 人工改了计划 |
| `session_saved` / `session_saved_error` | 存档成功/失败 |
| `session_resumed` | 恢复成功 |
| `takeover` | 人工接管 |
| `go` | 继续执行 |
| `max_iterations` | 转满轮次 |
| `clear` | /clear |
| `model_error` | 调用失败 |

## 成本

| 名词 | 一句话 | 落在哪 |
|---|---|---|
| **成本 ≈ 轮数 × 历史长度** | agent 比聊天贵，因为一轮要调很多次 | 设计动机 |
| **输入重发的代价** | 每轮都把历史全发一遍，历史越长越贵 | V2 起 |
| **输出比输入贵** | 多数模型如此，所以「三句话以内」是省钱开关 | 实测 813→151 tokens |
| **预算 budget** | 会话级 token 上限 | `100000` |
| **80% 告警** | 提前提醒，不是到 100% 才说 | `WARN_RATIO = 0.8` |
| **只告警一次** | `_warned` 标记，避免每轮刷屏 | `budget.py` |
| **超限返回字符串而非抛异常** | 预算用完是正常业务，不是程序错误 | `"ok"/"warning"/"exhausted"` |
| **环境变量写错不崩** | `int("abc")` 失败退回默认值 | `budget.py` 的 try |

---

# L8 工程与设计原则

| 名词 | 一句话 | 落在哪 |
|---|---|---|
| **Rule of Three** | 重复第三次才值得抽象 | V0/V1/V2 三份 → V3 收口 |
| **抽象/收口** | 把重复逻辑搬到公共模块 | `agent/client.py` |
| **单一职责** | 一个文件只干一件事 | `sandbox` / `logger` / `budget` 各自独立 |
| **分层** | 入口薄、公共模块厚 | V3 只有 137 行，逻辑在 `agent/` |
| **复用 vs 复制** | V4 用 `TOOLS_V3 + [...]` 做加法，不重抄 | `tools_v4.py` |
| **加法式扩展** | 新版本只加差异，不改老实现 | V6 直接 import V5 |
| **依赖注入** | 把「会被替换的东西」做成参数 | `ask` / `log` / `max_iterations` |
| **可测试性** | 能注入假 input，就不用人工按 y | `ask=None` |
| **向后兼容** | 新参数有默认值，老调用行为不变 | `max_iterations=None` |
| **输出不变原则** | 讲义里贴过的输出不能悄悄改 | `soft_landing` 分支才改文案 |
| **版本演进** | V0→V6 每版只解决一个问题 | 7 个文件 |
| **可运行的最小完整** | 每版都能独立跑，不依赖后续版本 | — |
| **不引入框架** | 不用 LangChain/CrewAI，保留「每行都能讲」的优势 | `pyproject.toml` 只有 requests |
| **依赖最小化** | 唯一第三方依赖 `requests` | — |
| **失败可观测** | 崩溃也要留下现场 | `try/finally` + 日志 |
| **错误信息要具体** | 「共 N 个任务」帮模型自我修正 | `Plan.find` |
| **文档即注释** | 文件头写「读完后该能回答什么」 | 16 个文件都有 |
| **注释与逻辑分离校验** | 用 AST 比对证明没改逻辑 | 我的 `verify_annotate.py` |

---

# L9 生态名词（本项目没用，但要知道）

| 名词 | 一句话 |
|---|---|
| **LangChain / LlamaIndex** | 最主流的 LLM 应用框架，抽象多、版本乱 |
| **CrewAI / AutoGen** | 多 agent 协作框架 |
| **MCP（Model Context Protocol）** | Anthropic 推的标准化工具接入协议 |
| **Function Calling** | OpenAI 对 tool use 的叫法，本质一样 |
| **ReAct** | Reason + Act，思考与行动交替的经典范式 |
| **RAG（检索增强生成）** | 先从知识库检索，再塞进上下文回答 |
| **embedding** | 把文本变成向量，用于语义检索 |
| **向量数据库** | Milvus / Qdrant / pgvector 等，存 embedding |
| **BM25** | 传统关键词检索算法，RAG 的轻量起点 |
| **切块 chunking** | 把长文档切成适合检索的小段 |
| **agentic workflow** | 用固定流程串多个 LLM 调用，比自由 agent 可控 |
| **guardrails** | 对输入输出做过滤/校验的护栏层 |
| **observability / tracing** | LangSmith、LangFuse 之类，看 agent 内部每一步 |
| **eval / 评测** | 用固定任务集测改动是否变好（本项目缺） |
| **CI for prompts** | 把评测接进流水线，提示词改动自动跑 |
| **streaming** | 边生成边返回，体验好 |
| **多 agent / 子 agent** | 一个 agent 派活给另一个 |
| **tool_choice** | 强制/禁止模型调工具的参数 |
| **JSON mode** | 强制输出合法 JSON |
| **context window 压缩** | 历史太长时裁剪/摘要（本项目缺） |

---

# 逐版新增对照表

| 版本 | 新增的**能力** | 新增的**知识** |
|---|---|---|
| **V0** | 单轮问答 | HTTP POST / headers / JSON / 状态码 / token 用量 / system |
| **V1** | 工具循环 | tool_use / tool_result / input_schema / registry / agent loop |
| **V2** | 多轮记忆 | REPL / messages 常驻 / ModelError / 重试 / /clear |
| **V3** | 能写磁盘 | 沙箱 / resolve+is_relative_to / 确认闸门 / WRITE_TOOLS / 依赖注入 |
| **V4** | 6 工具 + 可观测 | rglob 搜索 / write_file 预览 / JSONL 日志 / 预算 / 四步提示词 |
| **V5** | 显式计划 + 恢复 | dataclass / 状态机 / 阶段隔离 / 两道闸门 / 原子写 / phase / 断点恢复 |
| **V6** | 人工接管 | 命令解析 / 轮次缩放 / 软着陆 / 过期事实 / 接管消息 |

**关键提醒**：V5/V6 **没有新增任何文件工具**。加工具的动作在 V4 就停了。

---

# 面试高频 30 词（按出现概率排）

1. **agent 循环** — 问模型 → 执行工具 → 回填 → 再问
2. **tool_use / tool_result 配对** — 靠 `tool_use_id`，不是靠顺序
3. **为什么 tool_result 是 user 角色** — 协议只有 user/assistant
4. **服务端无状态** — 记忆全在本地 `messages`
5. **上下文累积** — 每轮全量重发，所以越聊越贵
6. **token 计费** — input + output 都算
7. **stop_reason** — 循环的开关
8. **input_schema** — JSON Schema 描述参数
9. **工具描述决定选中率** — 模型看不到实现
10. **registry 白名单** — 决定模型能力的物理边界
11. **错误即数据** — 异常转文字喂回去，模型自我纠错
12. **沙箱** — `resolve` 再 `is_relative_to`
13. **符号链接攻击** — 必须 resolve 后再校验
14. **默认拒绝** — 只有明确 y 才执行
15. **两道闸门** — 计划审批 + 写操作确认，各管一层
16. **阶段隔离** — 规划只给只读工具，结构上不可能越权
17. **显式计划** — 计划变对象，可检查/可存/可恢复
18. **状态机** — 5 个状态，`failed` 不算完成
19. **断点恢复** — 现场落盘，模型无状态所以能接上
20. **原子写** — tmp + replace
21. **状态与对话不一致** — 内存改了，历史没改 → 模型罢工
22. **过期事实** — 历史里那条结论现在不成立了
23. **人工接管** — 补一条 user 消息，不用改 system
24. **预算保险丝** — 超限即停
25. **软着陆** — 剩余不够下一轮就提前收
26. **检查点稀疏** — 预算问题的一个真实缺陷
27. **结构化日志 JSONL** — 崩了也只丢最后一行
28. **人工介入面最小化** — 改计划的命令不给模型
29. **评测**（缺）— 「你怎么知道改动变好了」
30. **服务化**（缺）— 确认闸门不能阻塞，要变异步状态

其中 **29、30** 是现在这个项目答不了的，也是下一步要补的。

---

# 三份可以对照读的文档

| 文档 | 内容 |
|---|---|
| `docs/notes/v0.md` ~ `v6.md` | 每版的讲义 + 真实输出 + 失败演示 |
| `docs/superpowers/specs/2026-10-29-ark-agent-design.md` | 设计原则与验收判据 |
| `docs/superpowers/plans/2026-11-11-learning-plan.md` | 六阶段学习计划 |

读完这份清单，`README.md` 的「设计原则」那几节应该能一口气读懂了。
