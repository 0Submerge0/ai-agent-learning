"""V5 工具集：按阶段给工具。规划阶段只能读，执行阶段才能写。

════════════════════════════════════════════════════════════
【这个文件是整个项目「权限设计」最漂亮的地方】
它的核心不是「多给了几个工具」，而是**两个不同的工具清单**：

    PLANNING_TOOLS   = 3 个只读工具 + create_plan
    EXECUTION_TOOLS  = 6 个读写工具 + update_task

请求时传给模型的 tools 字段，就是这两个列表之一。
规划阶段，write_file 这个名字**根本不在请求里**——
模型想调也调不出来，不是靠提示词劝它别调。

【为什么这么做，而不是在提示词里写「规划阶段不要写文件」】
因为提示词是建议，模型可以不遵守。
工具清单是协议层的事实：请求里没有这个函数，它就没有可选项。
这一步把「安全」从「相信模型」变成了「结构上不可能」。
    详见 docs/notes/v5.md §5，或 README 的设计原则 4。

【两阶段各自要干嘛】
规划阶段：模型先勘察（list_dir / read_file / search_files），
          然后调 create_plan 提交任务列表。
执行阶段：模型按计划干活，每开始一个任务调 update_task(→in_progress)，
          干完调 update_task(→done)。

【读完这个文件你应该能回答】
1. READ_TOOLS 是从 TOOLS_V4 里「筛」出来的，为什么用筛而不是重写一遍？
2. run_planning_tool 除了正常执行，为什么还要拦截非法调用？
   （防御第二层：即使模型在历史里看到过 write_file，它试着调用也会被拒）
3. 两个函数的返回值不一样：一个返回 (文本, bool)，一个只返回文本。为什么？
4. 为什么 update_task 的结果里要附带 plan.render()？
════════════════════════════════════════════════════════════
"""

from .plan import Plan
from .tools_v4 import TOOLS_V4, run_tool as run_v4_tool, summarize_args

# ── 从 V4 的 6 个工具里筛出只读的 3 个 ──────────────────────
# 用集合成员判断来筛，而不是手工重写一遍定义。
# 好处：V4 改了工具描述，这里自动跟着变，不会不同步。
READ_ONLY_NAMES = {"list_dir", "read_file", "search_files"}
READ_TOOLS = [t for t in TOOLS_V4 if t["name"] in READ_ONLY_NAMES]

# V4 全部 6 个工具的名字，执行阶段要用来做校验
EXECUTION_NAMES = [t["name"] for t in TOOLS_V4]

# ── 规划阶段专用工具 ────────────────────────────────────────
CREATE_PLAN_TOOL = {
    "name": "create_plan",
    "description": "提交任务计划。只在规划阶段使用。任务是按执行顺序排列的字符串数组，3-8 个为宜，最多 12 个。",
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",           # 注意是数组，不是字符串
                "items": {"type": "string"},
                "description": "按顺序执行的任务，每个一句话、可验证",
            },
        },
        "required": ["tasks"],
    },
}

# ── 执行阶段专用工具 ────────────────────────────────────────
UPDATE_TASK_TOOL = {
    "name": "update_task",
    "description": "更新一个任务的执行状态。开始做时设为 in_progress，完成设为 done，失败设为 failed，不需要做设为 skipped。",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "任务编号，从 1 开始"},
            "status": {
                "type": "string",
                # enum 限制了可选值。模型给别的值会被协议层拒绝，
                # 同时 plan.update() 里还有一层校验，双保险。
                "enum": ["pending", "in_progress", "done", "failed", "skipped"],
            },
            "note": {"type": "string", "description": "可选备注，例如失败原因"},
        },
        "required": ["task_id", "status"],
    },
}

# ══════════════════════════════════════════════════════════════
# 两个清单。这就是「阶段隔离」的全部实现——两行。
# ══════════════════════════════════════════════════════════════
PLANNING_TOOLS = READ_TOOLS + [CREATE_PLAN_TOOL]     # 3 只读 + 交计划
EXECUTION_TOOLS = TOOLS_V4 + [UPDATE_TASK_TOOL]      # 6 读写 + 改状态


def run_planning_tool(name: str, args: dict, plan: Plan, log=None) -> tuple[str, bool]:
    """规划阶段的分发。返回 (结果文本, 是否成功创建了计划)。

    为什么返回值比 V4 多一个 bool？
    因为调用方（planning_phase）需要知道「计划到手了没有」来决定要不要退出循环。
    用 bool 而不是去检查 plan.tasks 是否非空，
    是因为「模型调了 create_plan 但参数非法」这种情况也算失败——
    那种情况下 plan.tasks 可能还是空的，但错误已经回给模型了，
    应该让它再试一次，而不是当成成功。

    这是「防御第二层」：第一层是请求里不给写工具，
    第二层是即使模型硬调（比如历史里见过），这里也拒绝。
    """
    if name == "create_plan":
        try:
            plan.set_tasks(args.get("tasks") or [])
            if log:
                log("plan_created", tasks=len(plan.tasks), goal=plan.goal)
            # 把渲染后的计划回给模型：它下一轮就能看到
            # 「我的计划被接受成了这样」，序号和它提的一致。
            return "计划已创建：\n" + plan.render(), True
        except Exception as exc:
            # 参数非法（比如 15 个任务）→ 错误信息回给模型，让它自己改
            return f"工具执行失败：{exc}", False

    if name in READ_ONLY_NAMES:
        # 只读工具直接复用 V4 的实现
        return run_v4_tool(name, args, log=log), False

    # 兜底：模型试图在规划阶段调写工具
    return (
        f"规划阶段不能调用 {name}。可用：create_plan, "
        + ", ".join(sorted(READ_ONLY_NAMES))
    ), False


def run_execution_tool(name: str, args: dict, plan: Plan, ask=None, log=None) -> str:
    """执行阶段的分发。比规划阶段多一个 ask 参数（写操作要确认）。"""
    if name == "update_task":
        try:
            # 注意 int(...) 和 str(...) 的转换：
            # 模型给的 JSON 里 task_id 可能被写成字符串 "3"，
            # 顺手转一下比报错更友好。
            task = plan.update(
                int(args.get("task_id")),
                str(args.get("status")),
                args.get("note") or "",
            )
            if log:
                log("task_updated", task_id=task.id, status=task.status, note=task.note)
            # 返回里带上整个计划的渲染结果，是个有意的设计：
            # 模型每改一次状态，就重新看见一次完整计划，
            # 不容易忘记「还有哪几个没做」。
            return f"任务 {task.id} → {task.status}：{task.title}\n" + plan.render()
        except Exception as exc:
            return f"工具执行失败：{exc}"

    if name in EXECUTION_NAMES:
        # 6 个写/读工具走 V4 的实现，ask 传下去让确认闸门生效
        return run_v4_tool(name, args, ask=ask, log=log)

    return (
        f"执行阶段不能调用 {name}。可用：update_task, "
        + ", ".join(EXECUTION_NAMES)
    )


# __all__ 声明「这个模块对外提供哪些名字」。
# 写了它，from module import * 才只导入这些。
# 这里其实主要是文档作用：一眼看出模块的公开接口。
__all__ = [
    "CREATE_PLAN_TOOL",
    "UPDATE_TASK_TOOL",
    "PLANNING_TOOLS",
    "EXECUTION_TOOLS",
    "run_planning_tool",
    "run_execution_tool",
    "summarize_args",
]
