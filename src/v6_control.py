"""V6：计划可修改 + 人工接管。

V5 的计划批准后就锁死了，模型把任务标成 failed 你只能看着。V6 把计划交还给人：
/skip、/retry、/edit、/add、/rm 改计划，改完 /go 接着跑。

复用 v5_planner 的规划/审批/执行，只加一层命令解析。

════════════════════════════════════════════════════════════
【这个文件有多薄，说明 V5 的抽象有多成功】
282 行，其中真正「新」的逻辑只有三块：

    apply_plan_command()   解析 /skip /retry /edit /add /rm
    takeover_message()     接管时补一条新事实（这一版最重要的发现）
    iterations_for()       轮次上限跟着剩余任务数走

规划、审批、执行全部从 v5_planner import（第 16-22 行），一行没重写。

【V6 只做了两件事】
1. 把「改计划」的权力交给人
2. 修掉 V5 暴露的三个坑（轮次不够、预算硬停、收口说不清）

【最重要的一条设计原则，贯穿全文件】
    改计划的命令**不在**模型的工具清单里。
模型能改的只有任务状态（update_task），
它能看到的也不是这 5 个命令，而是每轮的 system prompt 里的 plan.render()。

为什么？因为计划是你批准的。它要是能自己加一步，
你批的 7 步就随时变成 8 步，V5 的两道闸门就漏了一道。
详见 docs/notes/v6.md §2.2。

【读完这个文件你应该能回答】
1. apply_plan_command 为什么返回三个值，而不是一个？
2. takeover_message 为什么不写进 system prompt，而是塞一条 user 消息？
3. 改完计划为什么要把 phase 从 "done" 拉回 "executing"？
4. 为什么 /go 每次都要重新构造接管消息，而不是只在第一次？
════════════════════════════════════════════════════════════
"""

import sys

from agent.budget import Budget
from agent.client import MODEL, ModelError
from agent.logger import SessionLogger
from agent.plan import Plan
from agent.session_store import SessionStore
# 注意：整个规划/审批/执行逻辑都是「借」来的，没有复制
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

# 会「改结构」的 5 个命令。它们只能由人输入，模型调不到。
PLAN_COMMANDS = ("/skip", "/retry", "/edit", "/add", "/rm")


# ══════════════════════════════════════════════════════════════
# 三个新函数
# ══════════════════════════════════════════════════════════════

def remaining_tasks(plan: Plan) -> int:
    """还有几个任务要做。

    注意只数 pending / in_progress：
    done 和 skipped 算完成，failed 不算「剩余」（它需要人工决定跳过还是重试），
    所以 failed 也不在这个计数里。
    """
    return sum(1 for t in plan.tasks if t.status in ("pending", "in_progress"))


def iterations_for(plan: Plan) -> int:
    """轮次上限跟着剩余任务走：每个任务至少两轮（标 in_progress + 干活）。

    V5 固定 16 轮，7 个任务实测会转满；这里给足，下界仍是 16。

    公式 max(16, 3 * 剩余 + 4) 的来历：
        3 * 剩余    每个任务平均要 3 轮（标 in_progress、干活、标 done）
        + 4         留给「收尾汇报 + 意外重试」
        下界 16     短任务不该被削到 5 轮，V5 的 16 是实测的安全值

    实测：剩余 3 个 → 16，7 个 → 25，12 个 → 40。
    """
    return max(16, 3 * remaining_tasks(plan) + 4)


def apply_plan_command(line: str, plan: Plan) -> tuple[bool, bool, str]:
    """处理改计划的命令。返回 (是不是这类命令, 有没有改动, 给用户看的话)。

    【为什么返回三个值】
    两个布尔值控制的是两件不同的事：
        handled  这行要不要被 REPL 吞掉
                 （True：它是命令，不该被当成新任务去规划）
        changed  要不要立刻存档
                 （False：报错了，什么都没变，不该写盘）

    典型组合：
        /skip 2    → (True, True,  "任务 2 已跳过…")      执行 + 存档
        /skip 99   → (True, False, "（没有编号为 99…）")  报错，不存档
        "帮我整理" → (False, False, "")                   不是命令，往下走

    这个设计让 REPL 的分支变得很干净：看 handled 决定吞不吞，
    看 changed 决定存不存。两个判断互不干扰。

    【为什么所有 ValueError 都就地转成中文】
    命令层是人和程序打交道的地方，抛异常会让 REPL 崩掉。
    一个手滑打错的命令不该让整个会话结束。
    """
    # maxsplit=2：最多切三份。
    # "/edit 3 新的标题" → ["/edit", "3", "新的标题"]（标题里再有空格也留得住）
    parts = line.split(maxsplit=2)
    cmd = parts[0]
    if cmd not in PLAN_COMMANDS:
        return False, False, ""

    # /add 的标题允许带空格，所以整段取出来。
    # 不能像 /edit 那样用 parts[2]——"/add 生成 一份 报告" 会被切碎。
    if cmd == "/add":
        title = line[len(cmd):].strip()
        try:
            task = plan.add(title)
            return True, True, f"已新增任务 {task.id}：{task.title}"
        except ValueError as exc:
            return True, False, f"（{exc}）"

    # 后面的命令都至少需要一个编号
    if len(parts) < 2:
        return True, False, f"（{cmd} 需要任务编号，例如 {cmd} 2）"

    try:
        task_id = int(parts[1])
    except ValueError:
        # {parts[1]!r} 会把输入原样带引号显示，用户能看出自己打了什么
        return True, False, f"（任务编号要是数字，收到：{parts[1]!r}）"

    # 从这里开始，任务编号合法了，剩下的是「编号对应的任务存不存在」
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
            # /edit 需要两段：编号 + 新标题
            if len(parts) < 3 or not parts[2].strip():
                return True, False, f"（/edit 需要新标题，例如 /edit {task_id} 新的任务描述）"
            task = plan.rename(task_id, parts[2])
            return True, True, f"任务 {task.id} 已改名：{task.title}"
    except ValueError as exc:
        # plan.find() 抛的「没有编号为 N 的任务」会走到这里
        return True, False, f"（{exc}）"

    return True, False, ""


def takeover_message(plan: Plan, budget: Budget, edits: list[str]) -> str:
    """人工接管时补一条新事实。

    恢复出来的历史里可能躺着「预算已用完，工具未执行」这种过期结论，
    模型会信它、拒绝动手。光改计划状态不够，得当面告诉它「情况变了」。

    ══════════════════════════════════════════════════════════
    【这个函数是 V6 最有价值的发现，值得单独理解】

    第一次跑演示时的现象：
        /retry 4   → 成功
        /skip 6    → 成功
        /go        → 模型连写三篇「我干不了」，一个工具都没调
        再 /go     → 又是「我干不了」
        再 /go     → 还是
    白花 800 多 token。

    原因：
        恢复出来的 messages 最后一条是
            tool_result: "会话预算已用完，工具未执行。"
        这条事实真的发生过，但它现在过期了。
        模型只能看见历史，看不见内存里的 Budget 还剩 57000。

    结论（比这个函数重要）：
        **plan / budget 和 messages 是两套东西，它们会不一致。**
        每次人工干预，都要把改了的事实写成一条消息发进去，
        而不是只改内存里的对象。

    为什么是 user 消息，不是改 system prompt？
        system prompt 每轮都重发，写进它会变成永久指令
        （「你总是被人工接管」）。
        而这件事只在这一次接管时成立。
        用 user 消息 = 一次性的新事实，符合对话语义。
    ══════════════════════════════════════════════════════════
    """
    parts = ["（人工接管）请继续执行计划，不要重复已完成的任务。"]
    # 把刚才人工做了哪些调整说清楚（"/skip 6" 这类原话）
    if edits:
        parts.append("刚才人工做的调整：" + "；".join(edits) + "。")
    # 最关键的一句：明确否定那条过期结论
    parts.append(
        f"当前进度：{plan.progress_text()}；剩余预算 {budget.remaining} tokens，"
        "工具可以正常调用（之前那条「预算已用完」是旧的，已经过期）。"
    )
    # 再贴一遍最新计划——因为 /rm 之后编号可能变了
    parts.append("当前计划：\n" + plan.render())
    parts.append("只处理状态为 pending / in_progress 的任务；done / skipped 的不要重做。")
    return "\n".join(parts)


def go(goal, plan, messages, budget, store, log, edits=None) -> str:
    """按当前（可能刚被人改过的）计划继续执行，返回阶段。

    和 V5 的「继续」相比多了两件事：
    1. 先补一条接管消息（见 takeover_message 的说明）
    2. 传 max_iterations / soft_landing，走 V6 的改进路径
    """
    if plan is None:
        print("（当前没有计划：先描述一个任务，或 /resume 恢复一个会话）")
        return "idle"
    if plan.is_complete:
        # 提前拦住，不用白跑一趟 execution_phase
        print("（计划已全部完成，没有要执行的任务）")
        return "done"

    # 只有「接着跑」时才需要接管消息。
    # messages 为空说明是全新任务，没有过期历史需要纠正。
    if messages:
        note = takeover_message(plan, budget, edits or [])
        messages.append({"role": "user", "content": note})
        log("takeover", edits=edits or [], chars=len(note), remaining=budget.remaining)

    limit = iterations_for(plan)
    print(f"（继续执行：剩余 {remaining_tasks(plan)} 个任务，本轮上限 {limit} 次循环）")
    log("go", tasks=remaining_tasks(plan), limit=limit)
    # 这两个参数就是 V6 相对 V5 的全部改动：
    #   max_iterations  轮次跟着任务数走
    #   soft_landing    预算不够就提前收，别硬停
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


# ══════════════════════════════════════════════════════════════
# REPL
# ══════════════════════════════════════════════════════════════

def run_repl() -> None:
    """V6 的交互循环。

    和 V5 的 REPL 结构一样，只是多了：
      - 5 个改计划的命令分支
      - /go（V5 只有「继续」）
      - edits 列表：记录这次接管前做过哪些调整，喂给 takeover_message
    """
    logger = SessionLogger()
    budget = Budget()
    store = SessionStore()
    messages: list = []
    plan: Plan | None = None
    goal = ""
    phase = "idle"
    # 攒着「本次接管做过哪些改动」，/go 时一起告诉模型，用完清空
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

            # ── /clear ──────────────────────────────────────
            if line == "/clear":
                messages = []
                plan = None
                goal = ""
                phase = "idle"
                edits = []  # 调整记录也清掉
                budget.reset()
                store = SessionStore()
                logger.log("clear")
                print("（记忆、计划、预算已清空；旧存档仍可用 /resume 恢复）")
                continue

            # ── /plan ───────────────────────────────────────
            if line == "/plan":
                if plan is None:
                    print("（当前没有计划）")
                else:
                    print(plan.render())
                    # V6 多一行分项：让人一眼看出「还剩几个 failed」
                    print(f"分项：{plan.summary() or '（空）'}")
                continue

            # ── /resume ─────────────────────────────────────
            if line == "/resume":
                loaded = load_resume(budget, store, logger.log)
                if loaded:
                    goal, phase, plan, messages = loaded
                    # 刚恢复，还没有任何人工调整
                    edits = []
                    print("输入 /go（或「继续」）接着执行；改计划用 /skip /retry /edit /add /rm。")
                continue

            # ── /go 或「继续」───────────────────────────────
            # 「继续」保留 V5 的习惯，两者完全等价
            if line in ("/go", "继续"):
                if plan is None:
                    print("（当前没有计划：先描述一个任务，或 /resume 恢复一个会话）")
                    continue
                try:
                    phase = go(goal, plan, messages, budget, store, logger.log, edits=edits)
                    # 调整记录已经告诉模型了，清空避免下次重复说
                    edits = []
                except ModelError as exc:
                    logger.log("model_error", phase=phase, error=str(exc))
                    print(f"调用失败：{exc}")
                    print("（计划还在，可以再 /go 一次）")
                    # 注意这里没有清空 edits：失败时调整还没送达，下次还要带
                continue

            # ── 改计划的命令 ────────────────────────────────
            # 改完立刻存档——改完就退出也不该丢。
            #
            # 为什么要「立刻」而不是等 /go 再存？
            # 因为改计划是人的操作，可能发生在任何时候：
            # 「我现在不想继续，先改一下明天再做」。
            # 第一版把 save 写在 /go 里，结果 /skip 6 → /exit → /resume，
            # 任务 6 又回来了。
            cmd = line.split(maxsplit=1)[0]
            if cmd in PLAN_COMMANDS:
                if plan is None:
                    print("（当前没有计划：先描述一个任务，或 /resume 恢复一个会话）")
                    continue
                _, changed, msg = apply_plan_command(line, plan)
                print(msg)
                if changed:
                    # 记下这次调整，/go 时告诉模型
                    edits.append(line)
                    # 计划又有活了，把阶段从 done 拉回 executing，否则 /resume 会说「已完成」。
                    #
                    # 这个细节很容易漏：
                    # V5 里计划完成时 phase = "done"，
                    # 而 load_resume 遇到 done 会直接说「已经完成，不需要继续」。
                    # 你 /add 一个任务，阶段却还是 done，/resume 就把你挡在门外。
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

            # ══ 新任务：重新规划（和 V5 一样，一条新消息 = 一个新任务）══
            if plan is not None and not plan.is_complete:
                print("（上一个计划还没收口；新输入会作为新任务重新规划，旧计划可用 /plan 查看）")
            goal = line
            messages = []
            plan = None
            phase = "planning"
            edits = []  # 新任务，旧的调整记录无效了
            logger.log("goal", chars=len(line))

            try:
                # 规划、审批、执行全部复用 v5_planner
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
                # 注意：这里调的是 V6 的 go()，不是 v5 的 execution_phase。
                # 差别在轮次缩放 + 软着陆 + 接管消息。
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
