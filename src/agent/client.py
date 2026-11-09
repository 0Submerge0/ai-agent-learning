"""唯一的模型调用封装。V0~V2 的发请求逻辑搬到这里。"""

import os
import sys

import requests

API_URL = "https://ark.cn-beijing.volces.com/api/coding/v1/messages"
MODEL = "deepseek-v4-1-flash-260910"
MAX_TOKENS = 1024


class ModelError(Exception):
    """模型调用失败。REPL 会捕获它并继续聊天，不让程序死掉。"""


def call_model(messages: list, tools: list, system: str, retries: int = 2) -> dict:
    # 有的环境里 key 会带尾随空格或换行（比如从文件里读出来），先清掉。
    api_key = (os.environ.get("ARK_API_KEY") or "").strip()
    if not api_key:
        raise ModelError("未找到 ARK_API_KEY 环境变量，请先设置后再运行。")

    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
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
            # 网络抖动：自动重试，而不是立刻放弃。
            if attempt < retries:
                print(f"（网络抖动，重试第 {attempt + 1} 次）", file=sys.stderr)
                continue
            raise ModelError(f"网络请求失败：{exc}")
        except requests.exceptions.RequestException as exc:
            # 异常信息里会带上 header 原值，也就是你的 key，所以要打码。
            raise ModelError("请求没有发出去：" + str(exc).replace(api_key, "***"))

        # 不用 raise_for_status()：要看到服务端返回的 error.code，才知道错在哪。
        if resp.status_code != 200:
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
