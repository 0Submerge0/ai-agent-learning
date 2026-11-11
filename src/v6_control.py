"""V6：计划可修改 + 人工接管。

V5 的计划批准后就锁死了，模型把任务标成 failed 你只能看着。V6 把计划交还给人：
/skip、/retry、/edit、/add、/rm 改计划，改完 /go 接着跑。

复用 v5_planner 的规划/审批/执行，只加一层命令解析。
"""

import sys

from agent.budget import Budget
from agent.client import MODEL, ModelError
from agent.logger import SessionLogger
from agent.plan import Plan
from agent.session_store import SessionStore
from v5_planner import (
    approve_plan,
    execution_phase,
    load_resume,
    planning_phase,
    save_session,
)

# Windows 控制台/管道默认不是 UTF-8，中文输出会乱码甚至报错。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

PLAN_COMMANDS = ("/skip", "/retry", "/edit", "/add", "/rm")


def remaining_tasks(plan: Plan) -> int:
    return sum(1 for t in plan.tasks if t.status in ("pending", "in_progress"))


def iterations_for(plan: Plan) -> int:
    """轮次上限跟着剩余任务走：每个任务至少两轮（标 in_progress + 干活）。

    V5 固定 16 轮，7 个任务实测会转满；这里给足，下界仍是 16。
    """
    return max(16, 3 * remaining_tasks(plan) + 4)


def apply_plan_command(line: str, plan: Plan) -> tuple[bool, bool, str]:
    """处理改计划的命令。返回 (是不是这类命令, 有没有改动, 给用户看的话)。"""
    parts = line.split(maxsplit=2)
    cmd = parts[0]
    if cmd not in PLAN_COMMANDS:
        return False, False, ""

    # /add 的标题允许带空格，所以整段取出来。
    if cmd == "/add":
        title = line[len(cmd):].strip()
        try:
            task = plan.add(title)
            return True, True, f"已新增任务 {task.id}：{task.title}"
        except ValueError as exc:
            return True, False, f"（{exc}）"

    if len(parts) < 2:
        return True, False, f"（{cmd} 需要任务编号，例如 {cmd} 2）"

    try:
        task_id = int(parts[1])
    except ValueError:
        return True, False, f"（任务编号要是数字，收到：{parts[1]!r}）"

    try:
        if cmd == "/skip":
            task = plan.skip(task_id)
            return True, True, f"任务 {task.id} 已跳过：{task.title}"
        if cmd == "/retry":
            task = plan.retry(task_id)
            return True, True, f"任务 {task.id} 已打回 pending：{task.title}"
        if cmd == "/rm":
            task = plan.remove(task_id)
            return True, True, f"已删除任务：{task.title}（后面的任务已重新编号）"
        if cmd == "/edit":
            if len(parts) < 3 or not parts[2].strip():
                return True, False, f"（/edit 需要新标题，例如 /edit {task_id} 新的任务描述）"
            task = plan.rename(task_id, parts[2])
            return True, True, f"任务 {task.id} 已改名：{task.title}"
    except ValueError as exc:
        return True, False, f"（{exc}）"

    return True, False, ""


def takeover_message(plan: Plan, budget: Budget, edits: list[str]) -> str:
    """人工接管时补一条新事实。

    恢复出来的历史里可能躺着「预算已用完，工具未执行」这种过期结论，
    模型会信它、拒绝动手。光改计划状态不够，得当面告诉它「情况变了」。
    """
    parts = ["（人工接管）请继续执行计划，不要重复已完成的任务。"]
    if edits:
        parts.append("刚才人工做的调整：" + "；".join(edits) + "。")
    parts.append(
        f"当前进度：{plan.progress_text()}；剩余预算 {budget.remaining} tokens，"
        "工具可以正常调用（之前那条「预算已用完」是旧的，已经过期）。"
    )
    parts.append("当前计划：\n" + plan.render())
    parts.append("只处理状态为 pending / in_progress 的任务；done / skipped 的不要重做。")
    return "\n".join(parts)


def go(goal, plan, messages, budget, store, log, edits=None) -> str:
    """按当前（可能刚被人改过的）计划继续执行，返回阶段。"""
    if plan is None:
        print("（当前没有计划：先描述一个任务，或 /resume 恢复一个会话）")
        return "idle"
    if plan.is_complete:
        print("（计划已全部完成，没有要执行的任务）")
        return "done"

    if messages:
        note = takeover_message(plan, budget, edits or [])
        messages.append({"role": "user", "content": note})
        log("takeover", edits=edits or [], chars=len(note), remaining=budget.remaining)

    limit = iterations_for(plan)
    print(f"（继续执行：剩余 {remaining_tasks(plan)} 个任务，本轮上限 {limit} 次循环）")
    log("go", tasks=remaining_tasks(plan), limit=limit)
    return execution_phase(
        goal,
        plan,
        messages,
        budget,
        store,
        log,
        max_iterations=limit,
        soft_landing=True,
    )


def run_repl() -> None:
    logger = SessionLogger()
    budget = Budget()
    store = SessionStore()
    messages: list = []
    plan: Plan | None = None
    goal = ""
    phase = "idle"
    edits: list[str] = []

    print("V6 计划控制版（可改计划 + 人工接管）。")
    print(f"日志：{logger.path}")
    print(f"预算：{budget.max_tokens} tokens（可用环境变量 MAX_SESSION_TOKENS 覆盖）。")
    print("改计划：/skip N 跳过，/retry N 重试，/edit N 标题 改名，/add 标题 新增，/rm N 删除。")
    print("跑计划：/go 继续执行，/plan 看进度，/resume 恢复，/clear 清空，/exit 退出。")
    logger.log("session_start", model=MODEL, max_tokens=budget.max_tokens, session_path=str(store.path))

    try:
        while True:
            try:
                line = input("你> ")
            except (EOFError, KeyboardInterrupt):
                break
            line = line.strip()
            if not line:
                continue
            if line == "/exit":
                break

            if line == "/clear":
                messages = []
                plan = None
                goal = ""
                phase = "idle"
                edits = []
                budget.reset()
                store = SessionStore()
                logger.log("clear")
                print("（记忆、计划、预算已清空；旧存档仍可用 /resume 恢复）")
                continue

            if line == "/plan":
                if plan is None:
                    print("（当前没有计划）")
                else:
                    print(plan.render())
                    print(f"分项：{plan.summary() or '（空）'}")
                continue

            if line == "/resume":
                loaded = load_resume(budget, store, logger.log)
                if loaded:
                    goal, phase, plan, messages = loaded
                    edits = []
                    print("输入 /go（或「继续」）接着执行；改计划用 /skip /retry /edit /add /rm。")
                continue

            if line in ("/go", "继续"):
                if plan is None:
                    print("（当前没有计划：先描述一个任务，或 /resume 恢复一个会话）")
                    continue
                try:
                    phase = go(goal, plan, messages, budget, store, logger.log, edits=edits)
                    edits = []
                except ModelError as exc:
                    logger.log("model_error", phase=phase, error=str(exc))
                    print(f"调用失败：{exc}")
                    print("（计划还在，可以再 /go 一次）")
                continue

            # 改计划的命令。改完立刻存档——改完就退出也不该丢。
            cmd = line.split(maxsplit=1)[0]
            if cmd in PLAN_COMMANDS:
                if plan is None:
                    print("（当前没有计划：先描述一个任务，或 /resume 恢复一个会话）")
                    continue
                _, changed, msg = apply_plan_command(line, plan)
                print(msg)
                if changed:
                    edits.append(line)
                    # 计划又有活了，把阶段从 done 拉回 executing，否则 /resume 会说「已完成」。
                    phase = "done" if plan.is_complete else "executing"
                    logger.log(
                        "plan_edited",
                        command=cmd,
                        phase=phase,
                        progress=plan.progress_text(),
                        summary=plan.summary(),
                    )
                    print(plan.render())
                    save_session(store, goal, phase, plan, messages, budget, logger.log)
                continue

            # 新任务：重新规划（和 V5 一样，一条新消息 = 一个新任务）。
            if plan is not None and not plan.is_complete:
                print("（上一个计划还没收口；新输入会作为新任务重新规划，旧计划可用 /plan 查看）")
            goal = line
            messages = []
            plan = None
            phase = "planning"
            edits = []
            logger.log("goal", chars=len(line))

            try:
                plan, messages = planning_phase(goal, budget, logger.log)
                if plan is None:
                    phase = "idle"
                    continue

                phase = "awaiting_approval"
                save_session(store, goal, phase, plan, messages, budget, logger.log)

                if not approve_plan(plan):
                    logger.log("plan_rejected", tasks=len(plan.tasks))
                    print("（计划未批准，已放弃；磁盘没有发生任何变化）")
                    save_session(store, goal, "rejected", None, [], budget, logger.log)
                    plan = None
                    messages = []
                    phase = "idle"
                    continue

                logger.log("plan_approved", tasks=len(plan.tasks))
                phase = "executing"
                save_session(store, goal, phase, plan, messages, budget, logger.log)
                phase = go(goal, plan, messages, budget, store, logger.log)
            except ModelError as exc:
                logger.log("model_error", phase=phase, error=str(exc))
                print(f"调用失败：{exc}")
                print("（可以再试一次；当前计划未丢失）")
    finally:
        logger.log(
            "session_end",
            phase=phase,
            used=budget.used,
            input_tokens=budget.input_tokens,
            output_tokens=budget.output_tokens,
            progress=plan.progress_text() if plan else None,
            summary=plan.summary() if plan else None,
            message_count=len(messages),
        )
        logger.close()


if __name__ == "__main__":
    run_repl()
