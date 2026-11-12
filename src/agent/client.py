"""唯一的模型调用封装。V0~V2 各自内联的发请求逻辑，从 V3 起搬到这里。

════════════════════════════════════════════════════════════
【它在整个项目里的位置】
这是整个项目的「底座」：V3~V6 全部通过 call_model() 发请求。
v0_chat.py 的 chat()、v1_tool.py 的 call_model()、v2_repl.py 的 call_model()
都有一份几乎一样的代码——那是「抽公共模块之前」的样子。

【为什么抽出来】
同一段逻辑（读 key、拼 payload、打请求、判错误）在三个文件里抄了三遍。
抄第三遍时，改一处漏两处就成了必然。V3 开始收口。

【这个文件比 V0 多了什么】
1. 自动重试（retries=2）：网络抖动时自己再试，不打扰用户
2. 统一的 ModelError：调用失败抛异常，让 REPL 决定怎么处理，
   而不是 sys.exit 把整个程序杀掉——这一点差别对 V2 之后的多轮对话很关键
════════════════════════════════════════════════════════════
"""

import os   # 读 ARK_API_KEY
import sys  # 往 stderr 打重试提示

import requests

API_URL = "https://ark.cn-beijing.volces.com/api/coding/v1/messages"
MODEL = "deepseek-v4-1-flash-260910"

# 比 V0 的 512 大：V3 之后模型要输出工具调用 + 计划文本，512 容易截断。
MAX_TOKENS = 1024


class ModelError(Exception):
    """模型调用失败。REPL 会捕获它并继续聊天，不让程序死掉。

    为什么自定义异常而不是直接用 requests 的异常？
    因为调用方只关心「这次调用失败了、原因是什么」，不关心是超时还是 401。
    统一成一种异常，上层的 try/except 只需要写一个分支。
    """


def call_model(messages: list, tools: list, system: str, retries: int = 2) -> dict:
    """调一次模型，返回解析后的响应 dict。

    参数：
        messages 完整对话历史（每次都是全量发过去，不是增量）
        tools    工具说明书列表；传空列表就不带 tools 字段
        system   system 提示词，每次请求都要重新带上
        retries  网络抖动时的重试次数（不含首次）

    失败时抛 ModelError，不返回 None——让调用方无法忽略错误。
    """
    # 有的环境里 key 会带尾随空格或换行（比如从文件里读出来），先清掉。
    api_key = (os.environ.get("ARK_API_KEY") or "").strip()
    if not api_key:
        raise ModelError("未找到 ARK_API_KEY 环境变量，请先设置后再运行。")

    # ── 拼请求体 ────────────────────────────────────────────
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": messages,
    }
    # 只有真的有工具时才带 tools 字段：空数组在部分服务端会被判成参数错误。
    if tools:
        payload["tools"] = tools

    # ── 带重试的请求循环 ────────────────────────────────────
    # range(retries + 1)：retries=2 时最多请求 3 次（首次 + 2 次重试）
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
            # 注意只重试这两类异常——它们代表「没拿到响应」，重试有意义。
            if attempt < retries:
                print(f"（网络抖动，重试第 {attempt + 1} 次）", file=sys.stderr)
                continue  # 回到 for 开头，再发一次
            raise ModelError(f"网络请求失败：{exc}")
        except requests.exceptions.RequestException as exc:
            # 这类异常说明请求构造有问题（比如 key 里有非法字符），
            # 重试多少次都一样，直接失败。
            # 异常信息里会带上 header 原值，也就是你的 key，所以要打码。
            raise ModelError("请求没有发出去：" + str(exc).replace(api_key, "***"))

        # 不用 raise_for_status()：要看到服务端返回的 error.code，才知道错在哪。
        # 注意：401/400/429 不重试——它们重试也不会变好，只会多花钱。
        if resp.status_code != 200:
            raise ModelError(f"请求失败：HTTP {resp.status_code} {resp.text[:300]}")

        try:
            return resp.json()
        except ValueError:
            # 代理、企业网关、机场 WiFi 的认证页都会返回 200 + HTML。
            raise ModelError("接口返回的不是 JSON（可能是代理或网关的拦截页）")

    # 理论上到不了这里（循环里每条路径都 return 或 raise），留着防止以后改坏。
    raise ModelError("多次重试仍失败，放弃本次请求。")


def extract_text(data: dict) -> str:
    """从响应里取出纯文本部分。

    content 是「块」的数组，可能有 thinking / text / tool_use 等多种类型。
    只拼 text 块——tool_use 由调用方单独处理。
    """
    blocks = data.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
