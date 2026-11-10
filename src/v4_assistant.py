"""V4：多工具助手。先勘察、再说计划、再执行、最后复查；全程记日志、按 token 计预算。"""

import json
import sys
import time

from agent.budget import Budget
from agent.client import MODEL, ModelError, call_model, extract_text
from agent.logger import SessionLogger
from agent.tools_v4 import TOOLS_V4, run_tool, summarize_args

# Windows 控制台/管道默认不是 UTF-8，中文输出会乱码甚至报错。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

SYSTEM_PROMPT = (
    "你是一个文件整理助手，只能操作 workspace 目录里的文件。"
    "处理任务时严格按四步走："
    "1) 先勘察：用 list_dir / search_files / read_file 了解现状，不要凭猜测回答；"
    "2) 再说计划：把准备做的步骤按顺序列出来；"
    "3) 再执行：需要写操作时调用工具，用户会在每一步确认；"
    "   互相独立的操作（比如移动多个文件）可以在一次回复里同时发起多个工具调用，不要一个一个来；"
    "4) 最后复查：用 list_dir / read_file 验证结果，再汇报做了什么。"
    "如果工具返回路径越界、目标已存在之类的错误，如实说明并调整，不要反复尝试同一个无效调用。"
)
MAX_ITERATIONS = 16


def log_tool_result(log, name: str, output: str) -> None:
    failed = output.startswith("工具执行失败") or output.startswith("用户拒绝了")
    log("tool_result", tool=name, ok=not failed, output_chars=len(output), output=output[:300])


def run_turn(messages: list, budget: Budget, log) -> list:
    """一次用户提问的完整循环：调模型 → 记用量 → 执行工具 → 回填 → 再来。"""
    for _ in range(MAX_ITERATIONS):
        # 预算用完就不再发请求，避免「知道超了还继续花」。
        if budget.used >= budget.max_tokens:
            print("（会话预算已用完，不再调用模型。输入 /clear 可以重置预算）")
            return messages

        log("model_request", message_count=len(messages))
        started = time.time()
        data = call_model(messages, TOOLS_V4, SYSTEM_PROMPT)
        elapsed_ms = round((time.time() - started) * 1000)

        stop = data.get("stop_reason")
        usage = data.get("usage") or {}
        log(
            "model_response",
            stop_reason=stop,
            elapsed_ms=elapsed_ms,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )

        # 原样回填，含 thinking 块。
        messages.append({"role": "assistant", "content": data["content"]})

        status = budget.add(usage)
        if status == "warning":
            print(f"（注意：本会话已用 {budget.status()}，接近上限）")
            log("budget_warning", used=budget.used, max_tokens=budget.max_tokens)
        elif status == "exhausted":
            # 如果这一轮模型已经发出工具请求，也要补上 tool_result，保持历史合法。
            pending = [b for b in data["content"] if b.get("type") == "tool_use"]
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
            print(f"（预算用完：{budget.status()}。本轮停止，未执行的工具不会执行；输入 /clear 重置）")
            log("budget_exhausted", used=budget.used, max_tokens=budget.max_tokens)
            return messages

        if stop == "tool_use":
            # 模型常常在发起工具调用的同时说一段计划，别把它吞掉——这正是 V4 想让人看见的。
            plan_text = extract_text(data)
            if plan_text.strip():
                print("助手> " + plan_text)
                log("plan_text", chars=len(plan_text))

            results = []
            for block in data["content"]:
                if block.get("type") != "tool_use":
                    continue
                args = block.get("input") or {}
                print(f"[工具] {block['name']}({json.dumps(args, ensure_ascii=False)})")
                log("tool_call", tool=block["name"], args=summarize_args(block["name"], args))
                output = run_tool(block["name"], args, log=log)
                print("       " + output.replace("\n", "\n       "))
                log_tool_result(log, block["name"], output)
                results.append(
                    {"type": "tool_result", "tool_use_id": block["id"], "content": output}
                )
            messages.append({"role": "user", "content": results})
            continue

        text = extract_text(data)
        print("助手> " + (text if text else f"（没有文本，stop={stop}）"))
        if stop == "max_tokens":
            print("（注意：回复被 max_tokens 截断了）")

        print(
            f"\n[用量] 本次输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')} tokens"
            f"；会话累计 {budget.status()}"
        )
        return messages

    print(f"（这一轮转满了 {MAX_ITERATIONS} 次工具循环，先停了）")
    log("max_iterations", limit=MAX_ITERATIONS)
    return messages


def run_repl() -> None:
    logger = SessionLogger()
    budget = Budget()
    messages = []  # 记忆本体：全程存活，每轮全量重发

    print("V4 多工具助手。只能操作 workspace。写操作会让你确认 (y/N)。")
    print(f"日志：{logger.path}")
    print(f"预算：{budget.max_tokens} tokens（可用环境变量 MAX_SESSION_TOKENS 覆盖）。")
    print("输入 /clear 清空记忆并重置预算，/exit 退出。")
    logger.log("session_start", model=MODEL, max_tokens=budget.max_tokens)

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
                budget.reset()
                logger.log("clear")
                print("（记忆已清空，预算已重置）")
                continue

            logger.log("user_message", chars=len(line))
            messages.append({"role": "user", "content": line})
            try:
                messages = run_turn(messages, budget, logger.log)
            except ModelError as exc:
                logger.log("model_error", error=str(exc))
                print(f"调用失败：{exc}")
                print("（可以再试一次，或者输入 /clear 重来）")
    finally:
        logger.log(
            "session_end",
            used=budget.used,
            input_tokens=budget.input_tokens,
            output_tokens=budget.output_tokens,
            message_count=len(messages),
        )
        logger.close()


if __name__ == "__main__":
    run_repl()
