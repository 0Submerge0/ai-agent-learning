"""V5：显式计划助手。先规划、等批准、再执行，计划可存档、可恢复。

════════════════════════════════════════════════════════════
【这一版把一次任务拆成了两个阶段】

    规划阶段  planning   →  模型只能用只读工具 + create_plan
        ↓  拿到计划，落盘，等用户批
    审批闸门  awaiting_approval  →  用户按 y 或 n
        ↓  y
    执行阶段  executing  →  模型用 6 个读写工具 + update_task
        ↓
    完成      done

为什么要拆？三个理由，一个比一个实际：

1. **安全**：规划时不碰磁盘。你批准的计划，就是将要发生的全部写入。
2. **可控**：批准之前你能看见全部步骤，而不是做到第 5 步才发现跑偏。
3. **可恢复**：计划是对象，能存成 JSON；执行到哪一步也是对象里的状态。

【这个文件的核心是三个函数】
    planning_phase()    第一段：只读 + 交计划
    approve_plan()      中间那道闸门
    execution_phase()   第二段：按计划干活

加上辅助：
    save_session()      每次状态变化都落盘
    load_resume()       从存档恢复现场

【读这个文件最容易迷路的地方】
`execution_phase` 有 110 行、8 个分支。别一口气读，
先看它返回什么（只有 "done" 和 "executing" 两种），
再看它从哪几个地方返回——一共 5 个出口：

    出口 1  预算用完（循环开头）          → executing
    出口 2  软着陆（V6 才有）             → executing
    出口 3  这一轮预算被耗尽（add 之后）  → executing
    出口 4  模型说话且计划完成            → done / executing
    出口 5  转满轮次                      → executing

注意：只有出口 4 可能返回 "done"。其他全是 "executing"——
意思是「没做完，但现场已保存」。

【和 V4 的本质区别】
V4 的每一轮都是「用户说一句 → 模型做一串 → 停下等下一句」。
V5 的 execution_phase 是「用户批准一次 → 模型自己连着做好几个任务 → 停下」。
多了「连续推进」的能力，代价是轮次和预算更容易撞墙。
════════════════════════════════════════════════════════════
"""

import json
import sys
import time

from agent.budget import Budget
from agent.client import MODEL, ModelError, call_model, extract_text
from agent.logger import SessionLogger
from agent.plan import Plan
from agent.session_store import SessionStore
from agent.tools_v5 import (
    EXECUTION_TOOLS,
    PLANNING_TOOLS,
    run_execution_tool,
    run_planning_tool,
    summarize_args,
)

# Windows 控制台/管道默认不是 UTF-8，中文输出会乱码甚至报错。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

# 规划阶段的轮次上限：勘察几个文件 + 交计划，6 轮通常够。
# 为什么比执行少？规划不写盘，反复勘察也没意义，卡住就是模型没理解目标。
PLANNING_MAX_ITERATIONS = 6

# 执行阶段的轮次上限。V5 固定 16。
# 实测 7 个任务会转满——V6 因此改成按剩余任务数缩放。
EXECUTION_MAX_ITERATIONS = 16


# ══════════════════════════════════════════════════════════════
# 两段提示词
# ══════════════════════════════════════════════════════════════

def planning_prompt(goal: str) -> str:
    """规划阶段的 system prompt。

    要点：明确告诉它「你没有写工具」。这不只是提醒——
    模型如果不知道，会浪费轮次去试 write_file，然后拿到报错。
    """
    return (
        "你是任务规划器。你只能勘察，不能修改文件。\n"
        f"用户目标：{goal}\n"
        "先用 list_dir / read_file / search_files 了解现状，再调用 create_plan 提交计划。\n"
        "计划要求：3-8 个任务（最多 12 个），按执行顺序排列，每个任务一句话、可验证。\n"
        "规划阶段没有写工具，不要尝试 write_file / mkdir / move_file。"
    )


def execution_prompt(goal: str, plan: Plan) -> str:
    """执行阶段的 system prompt。

    【关键设计：每轮重新渲染计划】
    看下面那句 {plan.render()}——这不是缓存的值，
    是每次调用 execution_prompt 时重新生成的。

    所以模型每一轮都能看到**最新的**任务状态。
    这就是为什么用户在 V6 里 /skip 一个任务后，
    模型下一轮就知道了——它读到的 system prompt 变了。

    代价：每轮都要多花几百 token 传计划文本。
    收益：模型不会忘记做到第几步。这笔买卖是值的。
    """
    return (
        "你是任务执行器。按用户已批准的计划执行，不能新增或重写计划。\n"
        f"用户目标：{goal}\n\n当前计划：\n{plan.render()}\n\n"
        "执行规则：\n"
        "1) 开始一个任务时调用 update_task 把它设为 in_progress；\n"
        "2) 完成后设为 done，失败设为 failed，确实不需要做才设为 skipped；\n"
        "3) 写操作需要用户确认，用户拒绝时如实说明并调整；\n"
        # 这一条是 V5 的「发动机」：让它自己连续推进，而不是每步停下等用户
        "4) 只要计划里还有 pending / in_progress 的任务，就继续调用工具推进，"
        "不要停在半路等用户说话；\n"
        "5) 全部完成后汇报，并核对计划里没有 pending / in_progress 的任务。\n"
        "如果工具返回错误，如实说明，不要反复尝试同一个无效调用。"
    )


# ══════════════════════════════════════════════════════════════
# 小工具函数
# ══════════════════════════════════════════════════════════════

def apply_budget(data: dict, budget: Budget, messages: list, log, phase: str):
    """把一次调用的用量记账，并在超限时停止。

    返回 (status, usage)：status 是 "ok"/"warning"/"exhausted"。

    为什么要把这段抽成函数？
    因为规划和执行两个阶段都要记账，逻辑完全一样。
    V4 里这段是直接写死在 run_turn 里的。

    【注意 exhausted 分支做的事】
    它往 messages 里补了一条「工具未执行」的 tool_result。
    这条消息很重要，也很有害——V6 那个坑就是从这来的：
      必要：不补的话，历史里有 tool_use 没有 tool_result，下次请求被服务端拒绝
      有害：这条是「过期事实」，模型下次读到会以为工具不可用，拒绝动手
    V6 的解法是接管时补一条新消息覆盖它。
    """
    usage = data.get("usage") or {}
    status = budget.add(usage)
    if status == "warning":
        print(f"（注意：本会话已用 {budget.status()}，接近上限）")
        log("budget_warning", phase=phase, used=budget.used, max_tokens=budget.max_tokens)
    elif status == "exhausted":
        # 已经发出 tool_use 的，补一条 tool_result，保持历史合法。
        pending = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
        if pending:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": b["id"],
                            "content": "会话预算已用完，工具未执行。",
                        }
                        for b in pending
                    ],
                }
            )
        print(f"（预算用完：{budget.status()}。本轮停止，未执行的工具不会执行）")
        log("budget_exhausted", phase=phase, used=budget.used, max_tokens=budget.max_tokens)
    return status, usage


def log_tool_result(log, phase: str, name: str, output: str) -> None:
    """记一条工具结果日志。失败判断靠字符串前缀（run_tool 的约定）。"""
    failed = output.startswith("工具执行失败") or output.startswith("用户拒绝了")
    log("tool_result", phase=phase, tool=name, ok=not failed, output_chars=len(output), output=output[:300])


def save_session(store, goal, phase, plan, messages, budget, log) -> None:
    """保存现场。任何可能中断的地方之前都要调它。

    注意 try/except：存档失败不该让整个任务崩掉。
    最坏情况是丢一次恢复点，而不是「活干完了但程序崩了」。
    只在终端提示一句，日志里记一笔。
    """
    try:
        store.save(goal=goal, phase=phase, plan=plan, messages=messages, budget=budget)
        log(
            "session_saved",
            phase=phase,
            path=str(store.path),
            progress=plan.progress_text() if plan else None,
        )
    except Exception as exc:
        log("session_save_error", error=str(exc))
        print(f"（会话保存失败：{exc}）")


# ══════════════════════════════════════════════════════════════
# 阶段一：规划
# ══════════════════════════════════════════════════════════════

def planning_phase(goal: str, budget: Budget, log) -> tuple[Plan | None, list]:
    """只读工具 + create_plan。拿到计划就返回，交给用户审批。

    返回 (plan, messages)：
        plan 为 None 表示没拿到计划（预算不够 / 转满轮次）
        messages 一定要返回，即使失败——它可能还要用来存草稿

    注意这里没有 save_session。
    为什么？因为规划阶段不碰磁盘，失败了重来代价很小，
    没必要为半成品计划留一堆存档。
    """
    plan = Plan(goal)
    # 规划阶段的历史从「用户目标」开始，是全新的列表。
    messages = [{"role": "user", "content": goal}]

    for _ in range(PLANNING_MAX_ITERATIONS):
        # 预算检查（和 V4 一样，在每轮开头）
        if budget.used >= budget.max_tokens:
            print("（会话预算已用完，无法继续规划。输入 /clear 重置）")
            return None, messages

        log("model_request", phase="planning", message_count=len(messages))
        started = time.time()
        # 注意第二个参数：PLANNING_TOOLS，只有 3 个只读 + create_plan
        data = call_model(messages, PLANNING_TOOLS, planning_prompt(goal))
        usage = data.get("usage") or {}
        log(
            "model_response",
            phase="planning",
            stop_reason=data.get("stop_reason"),
            elapsed_ms=round((time.time() - started) * 1000),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        # 原样回填 assistant 的回复（含 thinking 和 tool_use 块）
        messages.append({"role": "assistant", "content": data["content"]})

        status, _ = apply_budget(data, budget, messages, log, "planning")
        if status == "exhausted":
            return None, messages

        stop = data.get("stop_reason")
        text = extract_text(data)
        # 模型常常边勘察边评论，打印出来便于观察（也是教学价值）
        if text.strip():
            print("规划器> " + text)
            log("plan_text", phase="planning", chars=len(text))

        # ── 它没调工具，只是在说话 → 催它交计划 ─────────────
        if stop != "tool_use":
            print("（模型没有提交计划，提醒它使用 create_plan）")
            # 用一条 user 消息推它一把。这是「状态机驱动对话」的典型手法：
            # 需要什么就再说一遍，而不是重发整个请求。
            messages.append(
                {"role": "user", "content": "请调用 create_plan 提交任务列表，不要直接执行。"}
            )
            continue

        # ── 它调了工具（可能是勘察，也可能是 create_plan）────
        results = []
        created = False
        for block in data["content"]:
            if block.get("type") != "tool_use":
                continue
            args = block.get("input") or {}
            print(f"[规划工具] {block['name']}({json.dumps(args, ensure_ascii=False)})")
            log("tool_call", phase="planning", tool=block["name"], args=summarize_args(block["name"], args))
            # did_create 告诉我们这次调用是否真的建出了计划
            output, did_create = run_planning_tool(block["name"], args, plan, log=log)
            print("       " + output.replace("\n", "\n       "))
            log_tool_result(log, "planning", block["name"], output)
            results.append({"type": "tool_result", "tool_use_id": block["id"], "content": output})
            # 用 or 累积：一批里可能有多个 create_plan（不会，但这样写更稳）
            created = created or did_create
        messages.append({"role": "user", "content": results})

        # 计划到手了，本阶段结束
        if created:
            return plan, messages

    # 6 轮还没交出计划
    print(f"（规划阶段转了 {PLANNING_MAX_ITERATIONS} 轮仍没有得到计划，先停下）")
    log("planning_give_up", limit=PLANNING_MAX_ITERATIONS)
    return None, messages


# ══════════════════════════════════════════════════════════════
# 闸门一：计划审批
# ══════════════════════════════════════════════════════════════

def approve_plan(plan: Plan, ask=None) -> bool:
    """打印完整计划，问一句 y/N。这是第一道闸门。

    为什么要把整个计划打印出来？
    因为用户批的应该是「这 7 件事」，而不是「一个叫 plan 的东西」。
    看不见内容的批准等于没批准。

    ask=None 是为了可测试：测试时传一个假函数自动回答。
    和 tools.py 里的写法一样。
    """
    print("\n" + plan.render())
    ask = ask or input
    try:
        answer = ask("批准这个计划? (y/N) ")
    except EOFError:
        answer = ""
    # 只有明确 y 才算批准
    return answer.strip().lower() == "y"


# ══════════════════════════════════════════════════════════════
# 阶段二：执行
# ══════════════════════════════════════════════════════════════

def execution_phase(
    goal: str,
    plan: Plan,
    messages: list,
    budget: Budget,
    store,
    log,
    ask=None,
    max_iterations: int | None = None,
    soft_landing: bool = False,
) -> str:
    """按已批准的计划执行。返回结束时的阶段：done / executing。

    max_iterations / soft_landing 是 V6 加的可选参数：
    不传时行为和 V5 完全一致（16 轮、硬停）。

    【5 个出口，按代码出现顺序】
      1. 循环开头：预算已用完            → "executing"
      2. 循环开头：软着陆                → "executing"（仅 V6）
      3. 调完模型后：这一轮把预算吃光了  → "executing"
      4. 模型说话：计划完成 / 未完成     → "done" / "executing"  ← 唯一可能 done
      5. 转满轮次                        → "executing"

    除了 4，其余全部返回 "executing"，意思都是「没做完，但现场存好了」。
    """
    # 不传就用 V5 的常量 16。这里用 or 而不是 if None：
    # max_iterations=0 也会被当成 16，但没人会传 0，不必较真。
    limit = max_iterations or EXECUTION_MAX_ITERATIONS
    # 上一轮花了多少 token。软着陆靠它估算下一轮成本。
    last_cost = 0

    for _ in range(limit):
        # ═══ 出口 1：预算已经用完 ═══════════════════════════
        if budget.used >= budget.max_tokens:
            print("（会话预算已用完，不再调用模型。输入 /clear 重置，或 /resume 换个会话）")
            # 先存档再返回——这行是「断点恢复」能成立的原因
            save_session(store, goal, "executing", plan, messages, budget, log)
            return "executing"

        # ═══ 出口 2：软着陆（V6）════════════════════════════
        # 逻辑：上一轮花了 4000，这轮大概也要 4000。
        # 如果只剩 1500，与其冲进去被硬停（任务拦腰截断），不如现在就收。
        #
        # `last_cost and` 这个前置条件是必要的：
        # 第一轮时 last_cost 是 0，没有参考值，不能判断。
        if soft_landing and last_cost and budget.remaining < last_cost:
            print(
                f"（剩余预算 {budget.remaining} 不够下一轮（约 {last_cost}），提前收尾；"
                "进度已保存，可以 /go 接着做）"
            )
            log("soft_landing", phase="executing", remaining=budget.remaining, last_cost=last_cost)
            save_session(store, goal, "executing", plan, messages, budget, log)
            return "executing"

        # ── 调模型 ──────────────────────────────────────────
        log("model_request", phase="executing", message_count=len(messages))
        started = time.time()
        # 注意第二个参数：EXECUTION_TOOLS，6 个读写 + update_task
        # 第三个参数每轮重新渲染，模型因此能看到最新计划
        data = call_model(messages, EXECUTION_TOOLS, execution_prompt(goal, plan))
        usage = data.get("usage") or {}
        # 记下这一轮的实际成本，给下一轮的软着陆判断用
        last_cost = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        log(
            "model_response",
            phase="executing",
            stop_reason=data.get("stop_reason"),
            elapsed_ms=round((time.time() - started) * 1000),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
        messages.append({"role": "assistant", "content": data["content"]})

        # ═══ 出口 3：这一轮把预算吃光了 ═════════════════════
        status, _ = apply_budget(data, budget, messages, log, "executing")
        if status == "exhausted":
            save_session(store, goal, "executing", plan, messages, budget, log)
            return "executing"

        stop = data.get("stop_reason")
        text = extract_text(data)

        # ═══ 模型要调工具：执行 + 回填 + 继续下一轮 ═════════
        if stop == "tool_use":
            # 模型常边调工具边说计划，别吞掉
            if text.strip():
                print("助手> " + text)
                log("plan_text", phase="executing", chars=len(text))

            results = []
            updated = False  # 这一批里有没有真的改了任务状态
            for block in data["content"]:
                if block.get("type") != "tool_use":
                    continue
                args = block.get("input") or {}
                print(f"[工具] {block['name']}({json.dumps(args, ensure_ascii=False)})")
                log("tool_call", phase="executing", tool=block["name"], args=summarize_args(block["name"], args))
                # ask 一路传下去，让写操作的 y/N 闸门生效
                output = run_execution_tool(block["name"], args, plan, ask=ask, log=log)
                print("       " + output.replace("\n", "\n       "))
                log_tool_result(log, "executing", block["name"], output)
                results.append({"type": "tool_result", "tool_use_id": block["id"], "content": output})
                # 记下「状态变过」，好决定要不要打印进度
                if block["name"] == "update_task" and not output.startswith("工具执行失败"):
                    updated = True

            messages.append({"role": "user", "content": results})
            # 每一批工具执行完就存一次档。
            # 为什么这么频繁？因为工具执行是最可能出事的地方
            # （用户中途 Ctrl+C、磁盘满、程序崩）。
            save_session(store, goal, "executing", plan, messages, budget, log)
            if updated:
                print(f"（计划进度：{plan.progress_text()}）")
            continue  # 回到循环开头，让模型接着推进

        # ═══ 出口 4：模型只想说话 → 判断计划收没收口 ═════════
        print("助手> " + (text if text else f"（没有文本，stop={stop}）"))
        print("\n" + plan.render())

        if plan.is_complete:
            print("（计划已全部完成）")
            phase = "done"
        else:
            phase = "executing"
            if soft_landing:
                # V6：分项统计比「还有 N 个未完成」有用，failed 还能直接给出下一步。
                c = plan.counts()
                print(f"（计划未收口：{plan.progress_text()}；{plan.summary()}）")
                if c.get("failed"):
                    print(f"（其中 {c['failed']} 个 failed。输入 /retry N 重试，或 /skip N 跳过）")
            else:
                # V5 的原话，一个字都没改——讲义里贴过的输出不能悄悄变
                print(f"（注意：还有 {len(plan.tasks) - plan.done_count} 个任务未完成）")

        save_session(store, goal, phase, plan, messages, budget, log)
        print(
            f"\n[用量] 本次输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')} tokens"
            f"；会话累计 {budget.status()}"
        )
        return phase

    # ═══ 出口 5：转满了轮次 ═══════════════════════════════
    # hint 只在 V6 路径出现，保证 V5 输出不变
    hint = "；输入 /go 接着做" if soft_landing else ""
    print(f"（这一轮转满了 {limit} 次工具循环，计划可能还没做完{hint}）")
    log("max_iterations", phase="executing", limit=limit)
    save_session(store, goal, "executing", plan, messages, budget, log)
    return "executing"


# ══════════════════════════════════════════════════════════════
# 恢复
# ══════════════════════════════════════════════════════════════

def load_resume(budget: Budget, store: SessionStore, log):
    """读取最近一次存档。返回 (goal, phase, plan, messages) 或 None。

    【为什么能恢复】
    模型无状态。把 messages 原样发回去，它看到的历史和退出前一样。
    所以「恢复」这件事，本地做全了就行，不需要服务端配合。

    返回 None 的三种情况：
      1. 没有存档
      2. 存档读不出来（坏了）
      3. 计划已经完成了（没必要继续）

    注意第 3 种：它不是错误，是正常情况。
    """
    path = SessionStore.latest()
    if path is None:
        print("（没有找到可恢复的会话存档）")
        return None

    try:
        data = SessionStore.load(path)
    except Exception as exc:
        print(f"（读取存档失败：{exc}）")
        log("session_resume_error", path=str(path), error=str(exc))
        return None

    # ── 从 JSON 还原成对象 ──────────────────────────────────
    # plan 可能是 null（计划被拒绝时存的），所以要判断
    plan = Plan.from_dict(data["plan"]) if data.get("plan") else None
    messages = data.get("messages") or []
    # goal 优先用存档里的；没有就用计划里的目标兜底
    goal = data.get("goal") or (plan.goal if plan else "")
    phase = data.get("phase") or "idle"
    # 恢复用量（注意 restore 里「上限取较大值」的规则）
    budget.restore(data.get("budget"))
    # 接管这个文件，之后 save 会写回它，而不是新建
    store.adopt(path)

    log(
        "session_resumed",
        path=str(path),
        phase=phase,
        progress=plan.progress_text() if plan else None,
        used=budget.used,
    )
    print(f"已恢复会话：{path.name}")
    print(f"阶段：{phase}；预算：{budget.status()}")
    if plan:
        print(plan.render())

    # ── 判断能不能继续 ──────────────────────────────────────
    if plan is None or not messages:
        print("（这个会话里没有可继续的内容）")
        return None
    if phase == "done" or plan.is_complete:
        # 两个条件都查：phase 可能因为 bug 没更新，plan.is_complete 是更可靠的判断
        print("（这个计划已经完成，不需要继续）")
        return None

    return goal, phase, plan, messages


# ══════════════════════════════════════════════════════════════
# REPL
# ══════════════════════════════════════════════════════════════

def run_repl() -> None:
    """V5 的交互循环。

    和 V4 的 REPL 相比，多了这些状态变量和命令：
        plan           当前计划对象
        goal           当前目标
        phase          处在哪个阶段
        resume_ready   刚恢复过，输入「继续」就直接跑
        /plan          查看计划
        /resume        恢复存档

    注意 resume_ready 这个变量：它解决的是
    「恢复之后输入『继续』，应该接着跑而不是重新规划」这个问题。
    没有它的话，「继续」会被当成一个新任务，从头规划一遍——那就白恢复了。
    """
    logger = SessionLogger()
    budget = Budget()
    store = SessionStore()
    messages: list = []
    plan: Plan | None = None
    goal = ""
    phase = "idle"
    resume_ready = False

    print("V5 任务助手（显式计划版）。")
    print(f"日志：{logger.path}")
    print(f"预算：{budget.max_tokens} tokens（可用环境变量 MAX_SESSION_TOKENS 覆盖）。")
    print("每个新任务会先规划、等你批准，再执行；写操作仍会逐个确认。")
    print("命令：/plan 查看计划，/resume 恢复最近会话，/clear 清空，/exit 退出。")
    logger.log("session_start", model=MODEL, max_tokens=budget.max_tokens, session_path=str(store.path))

    try:
        while True:
            # ── 读一行 ──────────────────────────────────────
            try:
                line = input("你> ")
            except (EOFError, KeyboardInterrupt):  # Ctrl+D / Ctrl+C
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
                resume_ready = False
                budget.reset()
                # 换一个新的 store，这样下次存档是全新文件。
                # 旧存档还在磁盘上，仍可 /resume 恢复——这是有意的。
                store = SessionStore()
                logger.log("clear")
                print("（记忆、计划、预算已清空；旧存档仍可用 /resume 恢复）")
                continue

            # ── /plan ───────────────────────────────────────
            if line == "/plan":
                print(plan.render() if plan is not None else "（当前没有计划）")
                continue

            # ── /resume ─────────────────────────────────────
            if line == "/resume":
                loaded = load_resume(budget, store, logger.log)
                if loaded:
                    # 元组解包：load_resume 返回四个值
                    goal, phase, plan, messages = loaded
                    resume_ready = True
                    print("输入「继续」接着执行；输入其他内容会作为新任务重新规划。")
                continue

            # ── 「继续」= 恢复后接着跑 ───────────────────────
            # 注意三个条件同时成立才走这条：
            #   resume_ready   刚恢复过
            #   plan 不是 None 有计划
            # 少了任何一条，「继续」就会掉到下面「新任务」的分支里去
            if line == "继续" and resume_ready and plan is not None:
                try:
                    # 如果恢复时停在「等审批」，先补上这道闸门
                    if phase == "awaiting_approval":
                        if not approve_plan(plan):
                            logger.log("plan_rejected", tasks=len(plan.tasks), resumed=True)
                            print("（计划未批准，已放弃）")
                            plan = None
                            messages = []
                            phase = "idle"
                            resume_ready = False
                            continue
                        logger.log("plan_approved", tasks=len(plan.tasks), resumed=True)
                        phase = "executing"
                        save_session(store, goal, phase, plan, messages, budget, logger.log)
                    # 主体：接着执行
                    phase = execution_phase(goal, plan, messages, budget, store, logger.log)
                    # 还是 executing 才能继续「继续」
                    resume_ready = phase == "executing"
                except ModelError as exc:
                    logger.log("model_error", phase=phase, error=str(exc))
                    print(f"调用失败：{exc}")
                    # 失败时保持 resume_ready，让用户能再试一次
                    resume_ready = True
                continue

            # ══ 新任务：重新规划 ═════════════════════════════
            if plan is not None and not plan.is_complete:
                # 不阻止用户开新任务，但要提醒旧计划还没收口
                print("（上一个计划还有未完成任务；新输入会作为新任务重新规划，旧计划可用 /plan 查看）")

            # 重置所有状态：这是一条全新任务，旧历史不能带进来
            # （带了的话模型会看到上一个任务的文件列表，容易混淆）
            goal = line
            messages = []
            plan = None
            phase = "planning"
            resume_ready = False
            logger.log("goal", chars=len(line))

            try:
                # ── 阶段一：规划 ────────────────────────────
                plan, messages = planning_phase(goal, budget, logger.log)
                if plan is None:
                    phase = "idle"
                    continue

                # ── 存档：停在「等审批」─────────────────────
                # 这一步很重要：如果用户在审批时直接关窗口，
                # 下次 /resume 能看到这个待批的计划。
                phase = "awaiting_approval"
                save_session(store, goal, phase, plan, messages, budget, logger.log)

                # ── 闸门一 ─────────────────────────────────
                if not approve_plan(plan):
                    logger.log("plan_rejected", tasks=len(plan.tasks))
                    print("（计划未批准，已放弃；磁盘没有发生任何变化）")
                    # 存一个 rejected 状态，plan 存 None——
                    # 这样 /resume 不会又把这个被拒的计划捞出来
                    save_session(store, goal, "rejected", None, [], budget, logger.log)
                    plan = None
                    messages = []
                    phase = "idle"
                    continue

                # ── 阶段二：执行 ────────────────────────────
                logger.log("plan_approved", tasks=len(plan.tasks))
                phase = "executing"
                save_session(store, goal, phase, plan, messages, budget, logger.log)
                # 注意这里不传 max_iterations / soft_landing，
                # 所以走的是 V5 原本的 16 轮 + 硬停
                phase = execution_phase(goal, plan, messages, budget, store, logger.log)
                resume_ready = phase == "executing"
            except ModelError as exc:
                # 调用失败：计划还在，可以再试
                logger.log("model_error", phase=phase, error=str(exc))
                print(f"调用失败：{exc}")
                print("（可以再试一次；当前计划未丢失）")
                resume_ready = plan is not None and not plan.is_complete
    finally:
        # 无论怎么退出，都记一条收尾日志
        logger.log(
            "session_end",
            phase=phase,
            used=budget.used,
            input_tokens=budget.input_tokens,
            output_tokens=budget.output_tokens,
            progress=plan.progress_text() if plan else None,
            message_count=len(messages),
        )
        logger.close()


if __name__ == "__main__":
    run_repl()
