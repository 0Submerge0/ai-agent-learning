"""V5 工具集：按阶段给工具。规划阶段只能读，执行阶段才能写。"""

from .plan import Plan
from .tools_v4 import TOOLS_V4, run_tool as run_v4_tool, summarize_args

READ_ONLY_NAMES = {"list_dir", "read_file", "search_files"}
READ_TOOLS = [t for t in TOOLS_V4 if t["name"] in READ_ONLY_NAMES]
EXECUTION_NAMES = [t["name"] for t in TOOLS_V4]

CREATE_PLAN_TOOL = {
    "name": "create_plan",
    "description": "提交任务计划。只在规划阶段使用。任务是按执行顺序排列的字符串数组，3-8 个为宜，最多 12 个。",
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "按顺序执行的任务，每个一句话、可验证",
            },
        },
        "required": ["tasks"],
    },
}

UPDATE_TASK_TOOL = {
    "name": "update_task",
    "description": "更新一个任务的执行状态。开始做时设为 in_progress，完成设为 done，失败设为 failed，不需要做设为 skipped。",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer", "description": "任务编号，从 1 开始"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "done", "failed", "skipped"],
            },
            "note": {"type": "string", "description": "可选备注，例如失败原因"},
        },
        "required": ["task_id", "status"],
    },
}

PLANNING_TOOLS = READ_TOOLS + [CREATE_PLAN_TOOL]
EXECUTION_TOOLS = TOOLS_V4 + [UPDATE_TASK_TOOL]


def run_planning_tool(name: str, args: dict, plan: Plan, log=None) -> tuple[str, bool]:
    """返回 (结果文本, 是否成功创建了计划)。"""
    if name == "create_plan":
        try:
            plan.set_tasks(args.get("tasks") or [])
            if log:
                log("plan_created", tasks=len(plan.tasks), goal=plan.goal)
            return "计划已创建：\n" + plan.render(), True
        except Exception as exc:
            return f"工具执行失败：{exc}", False

    if name in READ_ONLY_NAMES:
        return run_v4_tool(name, args, log=log), False

    return (
        f"规划阶段不能调用 {name}。可用：create_plan, "
        + ", ".join(sorted(READ_ONLY_NAMES))
    ), False


def run_execution_tool(name: str, args: dict, plan: Plan, ask=None, log=None) -> str:
    if name == "update_task":
        try:
            task = plan.update(
                int(args.get("task_id")),
                str(args.get("status")),
                args.get("note") or "",
            )
            if log:
                log("task_updated", task_id=task.id, status=task.status, note=task.note)
            return f"任务 {task.id} → {task.status}：{task.title}\n" + plan.render()
        except Exception as exc:
            return f"工具执行失败：{exc}"

    if name in EXECUTION_NAMES:
        return run_v4_tool(name, args, ask=ask, log=log)

    return (
        f"执行阶段不能调用 {name}。可用：update_task, "
        + ", ".join(EXECUTION_NAMES)
    )


__all__ = [
    "CREATE_PLAN_TOOL",
    "UPDATE_TASK_TOOL",
    "PLANNING_TOOLS",
    "EXECUTION_TOOLS",
    "run_planning_tool",
    "run_execution_tool",
    "summarize_args",
]
