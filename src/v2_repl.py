"""V2：多轮对话与记忆。用 /clear 清空记忆，/exit 退出。"""

import json
import os
import sys
from pathlib import Path

import requests

# Windows 控制台/管道默认不是 UTF-8，中文输出会乱码甚至报错。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
# input() 从 stdin 读：管道/IDE 传进来的中文可能被按 GBK 解读，这里也统一成 UTF-8。
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

API_URL = "https://ark.cn-beijing.volces.com/api/coding/v1/messages"
MODEL = "deepseek-v4-1-flash-260910"
SYSTEM_PROMPT = "你是一个中文助手。需要了解文件情况时，先调用工具查看，不要凭空猜测。"
MAX_TOKENS = 1024
MAX_ITERATIONS = 5

# 工具定义：告诉模型「有这么个工具、它收什么参数」。
# 模型只会读这段描述，它看不到 list_dir 的代码。
TOOLS = [
    {
        "name": "list_dir",
        "description": "列出指定目录下的文件和子目录，返回名称与大小。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，例如 . 表示当前目录"},
            },
            "required": ["path"],
        },
    }
]


def list_dir(path: str) -> str:
    """真正干活的函数。模型永远碰不到它，只有我们的代码能调。"""
    p = Path(path).expanduser()
    if not p.exists():
        raise ValueError(f"路径不存在：{path}")
    if not p.is_dir():
        raise ValueError(f"不是目录：{path}")

    entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
    lines = []
    for e in entries[:50]:
        lines.append(f"{e.name}  <dir>" if e.is_dir() else f"{e.name}  {e.stat().st_size} B")
    if len(entries) > 50:
        lines.append(f"（已截断，共 {len(entries)} 条）")
    return "\n".join(lines) if lines else "（空目录）"


def run_tool(name: str, args: dict) -> str:
    """按名字分发。任何错误都变成一段文字回给模型，绝不让程序崩。"""
    registry = {"list_dir": list_dir}
    fn = registry.get(name)
    if fn is None:
        return f"没有名为 {name} 的工具，可用：{', '.join(registry)}"
    try:
        return fn(**args)
    except Exception as exc:
        return f"工具执行失败：{exc}"


class ModelError(Exception):
    """模型调用失败。REPL 会捕获它并继续聊天，不让程序死掉。"""


def call_model(messages: list, tools: list, retries: int = 2) -> dict:
    # 有的环境里 key 会带尾随空格或换行（比如从文件里读出来），先清掉。
    api_key = (os.environ.get("ARK_API_KEY") or "").strip()
    if not api_key:
        raise ModelError("未找到 ARK_API_KEY 环境变量，请先设置后再运行。")

    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools

    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                API_URL,
                headers={
                    # 注意：是 x-api-key，不是 Authorization: Bearer
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
                timeout=60,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            # 网络抖动：自动重试，而不是立刻放弃。这是「错误重试」的第一版。
            if attempt < retries:
                print(f"（网络抖动，重试第 {attempt + 1} 次）", file=sys.stderr)
                continue
            raise ModelError(f"网络请求失败：{exc}")
        except requests.exceptions.RequestException as exc:
            # 请求根本没发出去。异常信息里会带上 header 原值，也就是你的 key，所以要打码。
            raise ModelError("请求没有发出去：" + str(exc).replace(api_key, "***"))

        # 不用 raise_for_status()：要看到服务端返回的 error.code，才知道错在哪。
        if resp.status_code != 200:
            # 响应体截断，避免把服务端大段错误文本塞进终端。
            raise ModelError(f"请求失败：HTTP {resp.status_code} {resp.text[:300]}")

        try:
            return resp.json()
        except ValueError:
            # 代理、企业网关、机场 WiFi 的认证页都会返回 200 + HTML。
            raise ModelError("接口返回的不是 JSON（可能是代理或网关的拦截页）")

    raise ModelError("多次重试仍失败，放弃本次请求。")


def extract_text(data: dict) -> str:
    """content 是「块」的数组，可能有 thinking / text / tool_use 等多种类型。"""
    blocks = data.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def run_turn(messages: list) -> list:
    """处理一次用户提问。内部仍是 V1 的工具循环，区别是：
    1) 所有中间产物（模型回复、工具结果）都 append 进 messages 并保留；
    2) 失败抛 ModelError 而不是退出，由 REPL 决定怎么处理。
    返回更新后的历史。"""
    for _ in range(MAX_ITERATIONS):
        data = call_model(messages, TOOLS)
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
    print("V2 交互式助手。输入 /clear 清空记忆，/exit 退出。")
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
