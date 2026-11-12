"""V3：文件整理助手。只能操作 workspace，写操作前需 y 确认。

════════════════════════════════════════════════════════════
【这一版只有 91 行，但它是「能力边界」的转折点】
前两版，模型能做的只有 list_dir（只读，无害）。
V3 第一次让它能写磁盘：mkdir / move_file。

一旦能写，问题就全部变了：
    - 它能写到 D:\\Windows 去吗？        → sandbox.py 挡住
    - 它能在你不知情的时候移动文件吗？    → 确认闸门挡住

所以 V3 的代码虽然短，大部分逻辑都搬到了 agent/ 里：
    agent/client.py    发请求（V0~V2 内联的代码抽出来了）
    agent/sandbox.py   路径校验
    agent/tools.py     4 个工具 + 确认闸门

【这个文件本身只剩三件事】
1. SYSTEM_PROMPT：告诉模型它的职责和边界
2. run_turn：和 V2 一模一样的工具循环
3. run_repl：和 V2 一模一样的 REPL

也就是说：**V3 的新东西几乎全在 agent/ 目录里，入口文件反而变薄了。**
这是个好信号——说明抽象在起作用。

【读完这个文件你应该能回答】
1. 为什么「能写文件」这件事需要单独设计一道闸门？
2. 提示词里写「只能操作 workspace」有用吗？为什么还要代码校验？
   （提示词是建议，代码是强制。模型可以不听提示词，但绕不过 safe_path）
3. 为什么 V3 之后各版本都要复用 agent/client.py？
════════════════════════════════════════════════════════════
"""

import json
import sys

# 注意这三行 import：V3 开始用公共模块，不再自己内联 HTTP 代码。
from agent.client import ModelError, call_model, extract_text
from agent.tools import TOOLS, run_tool

# Windows 控制台/管道默认不是 UTF-8，中文输出会乱码甚至报错。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
# input() 从 stdin 读：管道/IDE 传进来的中文可能被按 GBK 解读，这里也统一成 UTF-8。
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

# ── 提示词：职责 + 边界 + 工作方式 ──────────────────────────
# 三句话分别对应三件事：
#   1. 你能碰什么（workspace）
#   2. 先看再说，别瞎猜（减少幻觉）
#   3. 写之前说计划，用户确认才执行（让闸门有意义——
#      如果模型不说要干什么，用户按 y 就是盲签）
SYSTEM_PROMPT = (
    "你是一个文件整理助手。你只能操作 workspace 目录里的文件。"
    "需要了解情况时先调用工具查看，不要凭猜测回答。"
    "移动文件或新建目录前，先把计划说清楚；用户输入 y 确认后才会真正执行。"
)

# 比 V2 多：V3 常常要「先列目录、再读几个文件、再移动」，5 轮不够用。
MAX_ITERATIONS = 8


def run_turn(messages: list) -> list:
    """处理一次用户提问。工具循环 + 原样回填 + 结果进历史，与 V2 相同。

    这段代码和 V2 第 132-171 行几乎一模一样。
    重复是有意的：每一版都保持「能独立读懂」，不为了去重把版本搞乱。
    """
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
                # 注意：这里没有传 ask，所以 run_tool 用默认的 input()。
                # 写操作会在 run_tool 内部停下来问 y/N。
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
    """和 V2 的 REPL 完全同构，只有提示语变了。"""
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
