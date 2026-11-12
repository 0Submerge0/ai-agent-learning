"""V4：多工具助手。先勘察、再说计划、再执行、最后复查；全程记日志、按 token 计预算。

════════════════════════════════════════════════════════════
【这一版加了三样东西，都不是新功能，而是「让 agent 能干活」】
1. 6 个工具（V3 只有 4 个）：加了 search_files 和 write_file
2. 日志：每一步落 JSONL，出问题能事后还原
3. 预算：按 token 计数，超限就停

【最重要的一处变化：提示词开始「教流程」】
V3 的提示词只说「你是文件整理助手，只能操作 workspace」。
V4 的提示词写死了四步：勘察 → 说计划 → 执行 → 复查。

为什么要教流程？因为实测发现模型会：
  - 不勘察就动手（然后移动到不存在的目录）
  - 一个一个文件地移（6 个文件吃 6 轮，容易撞上限）
  - 做完不复查（报告里写的和磁盘不一致）

所以提示词从「你是谁」变成了「你怎么工作」。

【另一处：预算用完时必须补 tool_result】
看第 68-83 行。这是个容易被忽略但很重要的细节——
模型发出 tool_use 之后，历史里必须有一条对应的 tool_result，
否则下一次请求会因「tool_use 没有响应」被服务端拒绝。
即使工具没执行，也要补一条「没执行」。

【读完这个文件你应该能回答】
1. 为什么 V4 的提示词要写「互相独立的操作可以在一次回复里同时发起」？
2. budget 检查放在循环开头，会带来什么问题？（V5/V6 的坑就从这里来）
3. log_tool_result 里判断失败用的是字符串前缀，这种写法有什么风险？
4. 为什么 plan_text 要单独抽出来打印，而不是丢掉？
════════════════════════════════════════════════════════════
"""

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

# ── 提示词：四步工作法 ──────────────────────────────────────
# 注意第 3 步里那句「可以在一次回复里同时发起多个工具调用」。
# 这不是随便加的：实测模型默认倾向「一次一个」，
# 7 个文件就是 7 轮，很容易撞上 MAX_ITERATIONS。
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

# 从 V3 的 8 提到 16。原因同上：工具变多，需要的轮次也变多。
MAX_ITERATIONS = 16


def log_tool_result(log, name: str, output: str) -> None:
    """记一条工具结果日志。

    判断失败的方式是「输出开头是不是某几个汉字」——
    因为 run_tool 把所有异常都转成了 "工具执行失败：..." 这样的字符串。
    这种「用字符串前缀判断状态」的写法很脆弱（改一个字就失效），
    但在学习项目里够用，而且省去了定义错误类型的工作。
    真正生产代码会返回结构化的结果。
    """
    failed = output.startswith("工具执行失败") or output.startswith("用户拒绝了")
    # 只记前 300 字符：完整输出可能是几千字的目录列表
    log("tool_result", tool=name, ok=not failed, output_chars=len(output), output=output[:300])


def run_turn(messages: list, budget: Budget, log) -> list:
    """一次用户提问的完整循环：调模型 → 记用量 → 执行工具 → 回填 → 再来。"""
    for _ in range(MAX_ITERATIONS):
        # ── 预算检查（在调模型之前）──────────────────────────
        # 预算用完就不再发请求，避免「知道超了还继续花」。
        #
        # 注意这个检查点的位置！它在「每轮开头」。
        # 如果某一轮本身花了 40000 token，而剩余只有 100，
        # 这一轮照样会跑完——检查点是稀的。
        # V6 的「软着陆」就是为了补这个洞。
        if budget.used >= budget.max_tokens:
            print("（会话预算已用完，不再调用模型。输入 /clear 可以重置预算）")
            return messages

        # ── 调模型（顺手记耗时）──────────────────────────────
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

        # ── 记账 + 处理预算状态 ──────────────────────────────
        status = budget.add(usage)
        if status == "warning":
            print(f"（注意：本会话已用 {budget.status()}，接近上限）")
            log("budget_warning", used=budget.used, max_tokens=budget.max_tokens)
        elif status == "exhausted":
            # 如果这一轮模型已经发出工具请求，也要补上 tool_result，保持历史合法。
            #
            # 为什么必须补？因为协议要求每个 tool_use 后面必须有一条
            # 对应的 tool_result。缺了的话下一次请求会被服务端拒绝。
            # 这一段是 V4 踩坑后加的，不是设计时就想到的。
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
            # 注意：这条「工具未执行」会永久留在历史里。
            # V6 实测发现它会让模型下次拒绝动手——这是 V6 那个坑的源头。
            print(f"（预算用完：{budget.status()}。本轮停止，未执行的工具不会执行；输入 /clear 重置）")
            log("budget_exhausted", used=budget.used, max_tokens=budget.max_tokens)
            return messages

        # ── 模型想调工具 ────────────────────────────────────
        if stop == "tool_use":
            # 模型常常在发起工具调用的同时说一段计划，别把它吞掉——这正是 V4 想让人看见的。
            # 这也是 V4 和 V5 的分界：V4 的「计划」只是这段话，
            # 代码里没有任何结构能检查它做到第几步。
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
                # summarize_args 把 write_file 的长内容换成「长度」，日志才不爆
                log("tool_call", tool=block["name"], args=summarize_args(block["name"], args))
                # log=log 传下去，让写操作能记 write_plan / write_confirm
                output = run_tool(block["name"], args, log=log)
                print("       " + output.replace("\n", "\n       "))
                log_tool_result(log, block["name"], output)
                results.append(
                    {"type": "tool_result", "tool_use_id": block["id"], "content": output}
                )
            messages.append({"role": "user", "content": results})
            continue

        # ── 模型只想说话 → 这一轮结束 ────────────────────────
        text = extract_text(data)
        print("助手> " + (text if text else f"（没有文本，stop={stop}）"))
        if stop == "max_tokens":
            print("（注意：回复被 max_tokens 截断了）")

        print(
            f"\n[用量] 本次输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')} tokens"
            f"；会话累计 {budget.status()}"
        )
        return messages

    # 转满 16 轮还没结束
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

    # try/finally 保证：即使中间抛异常，session_end 也会记上、文件也会关。
    # 少了这个，崩溃时日志就断在半空中，事后没法判断发生了什么。
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
                budget.reset()  # V4 起 /clear 不只清记忆，还把预算归零
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
        # 会话收尾：把最终用量和消息数记下来，这是成本复盘的数据来源
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
