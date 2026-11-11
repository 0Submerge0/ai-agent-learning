"""V5：显式计划助手。先规划、等批准、再执行，计划可存档、可恢复。"""

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

PLANNING_MAX_ITERATIONS = 6
EXECUTION_MAX_ITERATIONS = 16


def planning_prompt(goal: str) -> str:
    return (
        "你是任务规划器。你只能勘察，不能修改文件。\n"
        f"用户目标：{goal}\n"
        "先用 list_dir / read_file / search_files 了解现状，再调用 create_plan 提交计划。\n"
        "计划要求：3-8 个任务（最多 12 个），按执行顺序排列，每个任务一句话、可验证。\n"
        "规划阶段没有写工具，不要尝试 write_file / mkdir / move_file。"
    )


def execution_prompt(goal: str, plan: Plan) -> str:
    return (
        "你是任务执行器。按用户已批准的计划执行，不能新增或重写计划。\n"
        f"用户目标：{goal}\n\n当前计划：\n{plan.render()}\n\n"
        "执行规则：\n"
        "1) 开始一个任务时调用 update_task 把它设为 in_progress；\n"
        "2) 完成后设为 done，失败设为 failed，确实不需要做才设为 skipped；\n"
        "3) 写操作需要用户确认，用户拒绝时如实说明并调整；\n"
        "4) 只要计划里还有 pending / in_progress 的任务，就继续调用工具推进，"
        "不要停在半路等用户说话；\n"
        "5) 全部完成后汇报，并核对计划里没有 pending / in_progress 的任务。\n"
        "如果工具返回错误，如实说明，不要反复尝试同一个无效调用。"
    )


def apply_budget(data: dict, budget: Budget, messages: list, log, phase: str):
    """把一次调用的用量记账，并在超限时停止。"""
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
    failed = output.startswith("工具执行失败") or output.startswith("用户拒绝了")
    log("tool_result", phase=phase, tool=name, ok=not failed, output_chars=len(output), output=output[:300])


def save_session(store, goal, phase, plan, messages, budget, log) -> None:
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


def planning_phase(goal: str, budget: Budget, log) -> tuple[Plan | None, list]:
    """只读工具 + create_plan。拿到计划就返回，交给用户审批。"""
    plan = Plan(goal)
    messages = [{"role": "user", "content": goal}]

    for _ in range(PLANNING_MAX_ITERATIONS):
        if budget.used >= budget.max_tokens:
            print("（会话预算已用完，无法继续规划。输入 /clear 重置）")
            return None, messages

        log("model_request", phase="planning", message_count=len(messages))
        started = time.time()
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
        messages.append({"role": "assistant", "content": data["content"]})

        status, _ = apply_budget(data, budget, messages, log, "planning")
        if status == "exhausted":
            return None, messages

        stop = data.get("stop_reason")
        text = extract_text(data)
        if text.strip():
            print("规划器> " + text)
            log("plan_text", phase="planning", chars=len(text))

        if stop != "tool_use":
            print("（模型没有提交计划，提醒它使用 create_plan）")
            messages.append(
                {"role": "user", "content": "请调用 create_plan 提交任务列表，不要直接执行。"}
            )
            continue

        results = []
        created = False
        for block in data["content"]:
            if block.get("type") != "tool_use":
                continue
            args = block.get("input") or {}
            print(f"[规划工具] {block['name']}({json.dumps(args, ensure_ascii=False)})")
            log("tool_call", phase="planning", tool=block["name"], args=summarize_args(block["name"], args))
            output, did_create = run_planning_tool(block["name"], args, plan, log=log)
            print("       " + output.replace("\n", "\n       "))
            log_tool_result(log, "planning", block["name"], output)
            results.append({"type": "tool_result", "tool_use_id": block["id"], "content": output})
            created = created or did_create
        messages.append({"role": "user", "content": results})

        if created:
            return plan, messages

    print(f"（规划阶段转了 {PLANNING_MAX_ITERATIONS} 轮仍没有得到计划，先停下）")
    log("planning_give_up", limit=PLANNING_MAX_ITERATIONS)
    return None, messages


def approve_plan(plan: Plan, ask=None) -> bool:
    print("\n" + plan.render())
    ask = ask or input
    try:
        answer = ask("批准这个计划? (y/N) ")
    except EOFError:
        answer = ""
    return answer.strip().lower() == "y"


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
    """
    limit = max_iterations or EXECUTION_MAX_ITERATIONS
    last_cost = 0

    for _ in range(limit):
        if budget.used >= budget.max_tokens:
            print("（会话预算已用完，不再调用模型。输入 /clear 重置，或 /resume 换个会话）")
            save_session(store, goal, "executing", plan, messages, budget, log)
            return "executing"

        # 软着陆：下一轮大概还是要花这么多，不够就现在收，别把任务拦腰截断。
        if soft_landing and last_cost and budget.remaining < last_cost:
            print(
                f"（剩余预算 {budget.remaining} 不够下一轮（约 {last_cost}），提前收尾；"
                "进度已保存，可以 /go 接着做）"
            )
            log("soft_landing", phase="executing", remaining=budget.remaining, last_cost=last_cost)
            save_session(store, goal, "executing", plan, messages, budget, log)
            return "executing"

        log("model_request", phase="executing", message_count=len(messages))
        started = time.time()
        data = call_model(messages, EXECUTION_TOOLS, execution_prompt(goal, plan))
        usage = data.get("usage") or {}
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

        status, _ = apply_budget(data, budget, messages, log, "executing")
        if status == "exhausted":
            save_session(store, goal, "executing", plan, messages, budget, log)
            return "executing"

        stop = data.get("stop_reason")
        text = extract_text(data)

        if stop == "tool_use":
            if text.strip():
                print("助手> " + text)
                log("plan_text", phase="executing", chars=len(text))

            results = []
            updated = False
            for block in data["content"]:
                if block.get("type") != "tool_use":
                    continue
                args = block.get("input") or {}
                print(f"[工具] {block['name']}({json.dumps(args, ensure_ascii=False)})")
                log("tool_call", phase="executing", tool=block["name"], args=summarize_args(block["name"], args))
                output = run_execution_tool(block["name"], args, plan, ask=ask, log=log)
                print("       " + output.replace("\n", "\n       "))
                log_tool_result(log, "executing", block["name"], output)
                results.append({"type": "tool_result", "tool_use_id": block["id"], "content": output})
                if block["name"] == "update_task" and not output.startswith("工具执行失败"):
                    updated = True

            messages.append({"role": "user", "content": results})
            save_session(store, goal, "executing", plan, messages, budget, log)
            if updated:
                print(f"（计划进度：{plan.progress_text()}）")
            continue

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
                print(f"（注意：还有 {len(plan.tasks) - plan.done_count} 个任务未完成）")

        save_session(store, goal, phase, plan, messages, budget, log)
        print(
            f"\n[用量] 本次输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')} tokens"
            f"；会话累计 {budget.status()}"
        )
        return phase

    hint = "；输入 /go 接着做" if soft_landing else ""
    print(f"（这一轮转满了 {limit} 次工具循环，计划可能还没做完{hint}）")
    log("max_iterations", phase="executing", limit=limit)
    save_session(store, goal, "executing", plan, messages, budget, log)
    return "executing"


def load_resume(budget: Budget, store: SessionStore, log):
    """读取最近一次存档。返回 (goal, phase, plan, messages) 或 None。"""
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

    plan = Plan.from_dict(data["plan"]) if data.get("plan") else None
    messages = data.get("messages") or []
    goal = data.get("goal") or (plan.goal if plan else "")
    phase = data.get("phase") or "idle"
    budget.restore(data.get("budget"))
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

    if plan is None or not messages:
        print("（这个会话里没有可继续的内容）")
        return None
    if phase == "done" or plan.is_complete:
        print("（这个计划已经完成，不需要继续）")
        return None

    return goal, phase, plan, messages


def run_repl() -> None:
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
            try:
                line = input("你> ")
            except (EOFError, KeyboardInterrupt):  # Ctrl+D / Ctrl+C
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
                resume_ready = False
                budget.reset()
                store = SessionStore()
                logger.log("clear")
                print("（记忆、计划、预算已清空；旧存档仍可用 /resume 恢复）")
                continue
            if line == "/plan":
                print(plan.render() if plan is not None else "（当前没有计划）")
                continue
            if line == "/resume":
                loaded = load_resume(budget, store, logger.log)
                if loaded:
                    goal, phase, plan, messages = loaded
                    resume_ready = True
                    print("输入「继续」接着执行；输入其他内容会作为新任务重新规划。")
                continue

            # 恢复后输入「继续」：不再重新规划，直接接着执行。
            if line == "继续" and resume_ready and plan is not None:
                try:
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
                    phase = execution_phase(goal, plan, messages, budget, store, logger.log)
                    resume_ready = phase == "executing"
                except ModelError as exc:
                    logger.log("model_error", phase=phase, error=str(exc))
                    print(f"调用失败：{exc}")
                    resume_ready = True
                continue

            # 新任务：清空上一轮的对话，重新规划。
            if plan is not None and not plan.is_complete:
                print("（上一个计划还有未完成任务；新输入会作为新任务重新规划，旧计划可用 /plan 查看）")
            goal = line
            messages = []
            plan = None
            phase = "planning"
            resume_ready = False
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
                phase = execution_phase(goal, plan, messages, budget, store, logger.log)
                resume_ready = phase == "executing"
            except ModelError as exc:
                logger.log("model_error", phase=phase, error=str(exc))
                print(f"调用失败：{exc}")
                print("（可以再试一次；当前计划未丢失）")
                resume_ready = plan is not None and not plan.is_complete
    finally:
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
