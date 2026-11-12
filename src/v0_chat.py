"""V0：把一句话发给模型，打印回复。单轮、无记忆。

════════════════════════════════════════════════════════════
【它在整个项目里的位置】
这是最小的一版：90 行，只做一件事——用 requests 发一次 HTTP POST。
V1~V6 的所有能力都长在这一步之上，所以读代码从这里开始。

【读完这个文件你应该能回答】
1. 一次模型调用由哪几部分组成？（URL / headers / body）
2. 为什么 messages 是「数组」而不是一个字符串？
3. 模型的记忆为什么不在服务端？
4. token 用量为什么值得盯？

【它和 V2 的区别（最容易混的一点）】
V0 是「问一句、答一句、进程退出」，不保存任何历史。
V2 才有 REPL 和常驻的 messages 列表。
「V0 不能聊天」不是 bug，是这一版的设计。

【运行】
uv run python src/v0_chat.py "用一句话说你好"
（注意必须带 uv run；系统里的 python 是微软商店的占位程序，敲了只会静默退出）
════════════════════════════════════════════════════════════
"""

import os   # 读环境变量 ARK_API_KEY
import sys  # 读命令行参数、改 stdout 编码、sys.exit

import requests  # 唯一的第三方依赖。HTTP 请求全靠它

# Windows 控制台/管道默认不是 UTF-8，中文输出会乱码甚至报错。
# reconfigure 是 Python 3.7+ 的接口；hasattr 判断是为了兼容某些被重定向的流。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 接口地址：火山方舟的「Coding Plan」专用入口，走的是 Anthropic Messages 协议。
API_URL = "https://ark.cn-beijing.volces.com/api/coding/v1/messages"
MODEL = "deepseek-v4-1-flash-260910"

# system 提示词：模型的「岗位说明书」，每次请求都带上，不占 messages 的位置。
SYSTEM_PROMPT = "你是一个简洁的中文助手，回答控制在三句话以内。"

# 输出上限。注意它限制的是「模型这次能生成多少」，不是总上下文。
MAX_TOKENS = 512

# 命令行不给问题时用这句，方便直接跑通。
DEFAULT_QUESTION = "用一句话介绍你自己"


def build_payload(user_text: str) -> dict:
    """组装请求体（body）。

    返回的 dict 会被 requests 用 json= 参数序列化成 JSON。
    结构就是协议规定的四件套：model / max_tokens / system / messages。

    为什么 messages 是数组？
    因为一次对话可能有多种角色（user / assistant）和多轮内容，
    协议统一用「消息列表」表达。V0 只放一条，但形状必须对，
    否则 V2 想加历史时整个结构都要改。
    """
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_text}],
    }


def extract_text(data: dict) -> str:
    """从响应里取出纯文本。

    响应的 content 是一个「块数组」，每块有 type：
      - {"type": "text", "text": "..."}          → 我们要的正文
      - {"type": "thinking", ...}                → 模型的思考过程
      - {"type": "tool_use", ...}                → 它想调工具（V1 才处理）

    这里只拼 text 块，其余忽略。
    """
    blocks = data.get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def chat(user_text: str) -> None:
    """单轮问答：发一次请求，打印回复，结束。"""
    # 有的环境里 key 会带尾随空格或换行（比如从文件里读出来），先清掉。
    # 用 os.environ.get 而不是 os.environ[]，是为了缺变量时给友好提示而不是 KeyError。
    api_key = (os.environ.get("ARK_API_KEY") or "").strip()
    if not api_key:
        print("未找到 ARK_API_KEY 环境变量，请先设置后再运行。", file=sys.stderr)
        sys.exit(1)  # 退出码 1 = 失败，方便脚本判断

    # ── 发请求 ──────────────────────────────────────────────
    try:
        resp = requests.post(
            API_URL,
            headers={
                # 注意：是 x-api-key，不是 Authorization: Bearer。
                # 这是 Anthropic Messages 协议的约定，填错会直接 401。
                "x-api-key": api_key,
                # 协议版本号，服务端据此决定字段含义。
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=build_payload(user_text),  # 传 dict，requests 自动序列化
            timeout=60,                      # 必须有超时，否则网络卡住会一直挂着
        )
    # 网络层错误分两种，处理方式不同：
    # ① 超时 / 连不上 —— 可能是抖动，提示重跑即可
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        print(f"网络请求失败：{exc}", file=sys.stderr)
        print("可能是网络超时或未连通，重跑一次通常就好。", file=sys.stderr)
        sys.exit(1)
    # ② 其他请求异常 —— 请求根本没发出去
    except requests.exceptions.RequestException as exc:
        # 异常信息里会带上 header 原值，也就是你的 key，所以要打码。
        print("请求没有发出去：" + str(exc).replace(api_key, "***"), file=sys.stderr)
        print("请检查 ARK_API_KEY 是否含空格或换行。", file=sys.stderr)
        sys.exit(1)

    # ── 检查 HTTP 状态码 ────────────────────────────────────
    # 不用 raise_for_status()：要看到服务端返回的 error.code，才知道错在哪。
    # 401 = key 错/失效，400 = 参数错，429 = 限流，5xx = 服务端问题。
    if resp.status_code != 200:
        print(f"请求失败：HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text, file=sys.stderr)
        sys.exit(1)

    # ── 解析 JSON ───────────────────────────────────────────
    try:
        data = resp.json()
    except ValueError:
        # 代理、企业网关、机场 WiFi 的认证页都会返回 200 + HTML。
        # 这种情况下状态码是 200，但内容根本不是 JSON。
        print("接口返回的不是 JSON（可能是代理或网关的拦截页）：", file=sys.stderr)
        print(resp.text[:200], file=sys.stderr)
        sys.exit(1)

    # ── 打印结果 ────────────────────────────────────────────
    text = extract_text(data)
    if text:
        print(text)
    else:
        # 没有 text 块，通常是被 max_tokens 截断在 thinking 阶段了。
        print(f"（没有拿到文本，stop_reason={data.get('stop_reason')}）")

    # 用量是「输入 token（你发过去的）+ 输出 token（它生成的）」，
    # 两条都要算钱，所以都打印出来。
    usage = data.get("usage") or {}
    print(f"\n[用量] 输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')} tokens")


if __name__ == "__main__":
    # 只有直接运行这个文件时才执行（被 import 时不执行）。
    # 拼回整句：不加引号时 argv 会被空格切成好几段，全拼起来才不丢字。
    chat(" ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_QUESTION)
