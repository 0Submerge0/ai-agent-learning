"""V1：给模型一个工具，看它自己决定调用。"""

import json
import os
import sys
from pathlib import Path

import requests

# Windows 控制台/管道默认不是 UTF-8，中文输出会乱码甚至报错。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

API_URL = "https://ark.cn-beijing.volces.com/api/coding/v1/messages"
MODEL = "deepseek-v4-1-flash-260910"
SYSTEM_PROMPT = "你是一个中文助手。需要了解文件情况时，先调用工具查看，不要凭空猜测。"
MAX_TOKENS = 1024
MAX_ITERATIONS = 5
DEFAULT_QUESTION = "看看当前目录里有什么文件"

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


def call_model(messages: list, tools: list) -> dict:
    # 有的环境里 key 会带尾随空格或换行（比如从文件里读出来），先清掉。
    api_key = (os.environ.get("ARK_API_KEY") or "").strip()
    if not api_key:
        print("未找到 ARK_API_KEY 环境变量，请先设置后再运行。", file=sys.stderr)
        sys.exit(1)

    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools

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
        print(f"网络请求失败：{exc}", file=sys.stderr)
        print("可能是网络超时或未连通，重跑一次通常就好。", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.RequestException as exc:
        # 请求根本没发出去。异常信息里会带上 header 原值，也就是你的 key，所以要打码。
        print("请求没有发出去：" + str(exc).replace(api_key, "***"), file=sys.stderr)
        print("请检查 ARK_API_KEY 是否含空格或换行。", file=sys.stderr)
        sys.exit(1)

    # 不用 raise_for_status()：要看到服务端返回的 error.code，才知道错在哪。
    if resp.status_code != 200:
        print(f"请求失败：HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        sys.exit(1)

    try:
        return resp.json()
    except ValueError:
        # 代理、企业网关、机场 WiFi 的认证页都会返回 200 + HTML。
        print("接口返回的不是 JSON（可能是代理或网关的拦截页）：", file=sys.stderr)
        print(resp.text[:200], file=sys.stderr)
        sys.exit(1)


def extract_text(data: dict) -> str:
    """content 是「块」的数组，可能有 thinking / text / tool_use 等多种类型。"""
    blocks = data.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def run_agent(question: str) -> None:
    messages = [{"role": "user", "content": question}]

    for step in range(MAX_ITERATIONS):
        data = call_model(messages, TOOLS)
        stop = data.get("stop_reason")

        # 关键：assistant 的 content 必须原样回填，包括 thinking 块和它的 signature。
        # 只挑 text 块塞回去，下一轮会失败。
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
                # tool_result 靠 tool_use_id 配对，不是靠顺序。
                results.append(
                    {"type": "tool_result", "tool_use_id": block["id"], "content": output}
                )
            # 所有结果放在同一条 user 消息里；模型一次要调三个工具也只需回一条。
            messages.append({"role": "user", "content": results})
            continue

        print(extract_text(data))
        if stop == "max_tokens":
            print("（注意：回复被 max_tokens 截断了）")

        usage = data.get("usage") or {}
        print(f"\n[用量] 输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')} tokens")
        return

    print(f"已经转满 {MAX_ITERATIONS} 轮还没得出结论，先停下。")


if __name__ == "__main__":
    # 拼回整句：不加引号时 argv 会被空格切成好几段，全拼起来才不丢字。
    run_agent(" ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_QUESTION)
