"""V3：文件整理助手。只能操作 workspace，写操作前需 y 确认。"""

import json
import sys

from agent.client import ModelError, call_model, extract_text
from agent.tools import TOOLS, run_tool

# Windows 控制台/管道默认不是 UTF-8，中文输出会乱码甚至报错。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
# input() 从 stdin 读：管道/IDE 传进来的中文可能被按 GBK 解读，这里也统一成 UTF-8。
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

SYSTEM_PROMPT = (
    "你是一个文件整理助手。你只能操作 workspace 目录里的文件。"
    "需要了解情况时先调用工具查看，不要凭猜测回答。"
    "移动文件或新建目录前，先把计划说清楚；用户输入 y 确认后才会真正执行。"
)
MAX_ITERATIONS = 8


def run_turn(messages: list) -> list:
    """处理一次用户提问。工具循环 + 原样回填 + 结果进历史，与 V2 相同。"""
    for _ in range(MAX_ITERATIONS):
        data = call_model(messages, TOOLS, SYSTEM_PROMPT)
        stop = data.get("stop_reason")

        # 原样回填，含 thinking 块。
        messages.append({"role": "assistant", "content": data["content"]})

        if stop == "tool_use":
            results = []
            for block in data["content"]:
                if block.get("type") != "tool_use":
                    continue
                args = block.get("input") or {}
                print(f"[工具] {block['name']}({json.dumps(args, ensure_ascii=False)})")
                output = run_tool(block["name"], args)
                print("       " + output.replace("\n", "\n       "))
                results.append(
                    {"type": "tool_result", "tool_use_id": block["id"], "content": output}
                )
            messages.append({"role": "user", "content": results})
            continue

        text = extract_text(data)
        print("助手> " + (text if text else f"（没有文本，stop={stop}）"))
        if stop == "max_tokens":
            print("（注意：回复被 max_tokens 截断了）")

        usage = data.get("usage") or {}
        print(f"\n[用量] 本次输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')} tokens")
        if len(messages) > 20:
            print("（提示：历史已经很长，上下文费用在涨，随时可以输入 /clear）")
        return messages

    print(f"（这一轮转满了 {MAX_ITERATIONS} 次工具循环，先停了）")
    return messages


def run_repl() -> None:
    messages = []  # 这是「记忆」本体：全程存活，每轮全量重发
    print("V3 文件整理助手。只能操作 workspace。写操作会让你确认 (y/N)。输入 /clear 清空记忆，/exit 退出。")
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
            print("（记忆已清空）")
            continue

        messages.append({"role": "user", "content": line})
        try:
            messages = run_turn(messages)
        except ModelError as exc:
            print(f"调用失败：{exc}")
            print("（可以再试一次，或者输入 /clear 重来）")


if __name__ == "__main__":
    run_repl()
