"""V0：把一句话发给模型，打印回复。单轮、无记忆。"""

import os
import sys

import requests

# Windows 控制台/管道默认不是 UTF-8，中文输出会乱码甚至报错。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

API_URL = "https://ark.cn-beijing.volces.com/api/coding/v1/messages"
MODEL = "deepseek-v4-1-flash-260910"
SYSTEM_PROMPT = "你是一个简洁的中文助手，回答控制在三句话以内。"
MAX_TOKENS = 512
DEFAULT_QUESTION = "用一句话介绍你自己"


def build_payload(user_text: str) -> dict:
    """组装请求体。messages 是数组，因为一次对话可能有多轮、多种角色。"""
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_text}],
    }


def extract_text(data: dict) -> str:
    """content 是「块」的数组，可能有 thinking / text 等多种类型。"""
    blocks = data.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def chat(user_text: str) -> None:
    # 有的环境里 key 会带尾随空格或换行（比如从文件里读出来），先清掉。
    api_key = (os.environ.get("ARK_API_KEY") or "").strip()
    if not api_key:
        print("未找到 ARK_API_KEY 环境变量，请先设置后再运行。", file=sys.stderr)
        sys.exit(1)

    try:
        resp = requests.post(
            API_URL,
            headers={
                # 注意：是 x-api-key，不是 Authorization: Bearer
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=build_payload(user_text),
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
        data = resp.json()
    except ValueError:
        # 代理、企业网关、机场 WiFi 的认证页都会返回 200 + HTML。
        print("接口返回的不是 JSON（可能是代理或网关的拦截页）：", file=sys.stderr)
        print(resp.text[:200], file=sys.stderr)
        sys.exit(1)
    text = extract_text(data)
    if text:
        print(text)
    else:
        print(f"（没有拿到文本，stop_reason={data.get('stop_reason')}）")

    usage = data.get("usage") or {}
    print(f"\n[用量] 输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')} tokens")


if __name__ == "__main__":
    # 拼回整句：不加引号时 argv 会被空格切成好几段，全拼起来才不丢字。
    chat(" ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_QUESTION)
