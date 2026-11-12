"""V1：给模型一个工具，看它自己决定调用。

════════════════════════════════════════════════════════════
【这一版是「agent」诞生的地方】
V0 只是「问一句答一句」。V1 加了一个闭环：

    问模型 → 它说「我要调 list_dir」→ 我们执行 → 把结果喂回去 → 再问模型

这个「执行 → 回填 → 再问」就是 agent 的全部骨架。
后面 V2~V6 都只是给这个循环加外壳，循环本身一行没变。

【read_code 关键字：stop_reason == "tool_use"】
模型不直接动文件。它回一句「我想调这个函数，参数是这些」，
真正干活的是本文件下方的 list_dir()。
所以「模型能干坏事」这件事，本质是「run_tool 的分发表里有没有那个函数」。

【读完这个文件你应该能回答】
1. tools 那一段 JSON 是给谁看的？（模型）它看得到 list_dir 的实现吗？（看不到）
2. tool_result 为什么用 user 角色回填？
3. 循环什么时候退出？MAX_ITERATIONS 是防什么？
4. 为什么 assistant 的 content 要「原样」append 回去？

【运行】
uv run python src/v1_tool.py "看看当前目录里有什么文件"
════════════════════════════════════════════════════════════
"""

import json  # 只用来把工具参数打印成一行可读的 JSON
import os    # 读 ARK_API_KEY
import sys   # argv / 编码 / exit
from pathlib import Path  # 目录遍历

import requests

# Windows 控制台/管道默认不是 UTF-8，中文输出会乱码甚至报错。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

API_URL = "https://ark.cn-beijing.volces.com/api/coding/v1/messages"
MODEL = "deepseek-v4-1-flash-260910"

# 注意这句「先调用工具查看，不要凭空猜测」——
# 工具调用能不能触发，一半靠这句提示词，一半靠下面的 tools 定义。
SYSTEM_PROMPT = "你是一个中文助手。需要了解文件情况时，先调用工具查看，不要凭空猜测。"

MAX_TOKENS = 1024

# 防止「模型一直调工具不收敛」。比如它反复 list_dir 同一个目录，
# 没有上限就会无限花钱。V2 起这个值被调过好几次。
MAX_ITERATIONS = 5

DEFAULT_QUESTION = "看看当前目录里有什么文件"

# ══════════════════════════════════════════════════════════════
# 工具定义：告诉模型「有这么个工具、它收什么参数」。
#
# 这段 JSON 会被塞进请求体的 tools 字段，模型读了才知道能调什么。
# 模型只会读这段描述，它看不到 list_dir 的代码——
# 所以 description 写得准不准，直接决定它用得对不对。
#
# 三个字段缺一不可：
#   name         函数名，模型回传时要一模一样
#   description  什么时候用、返回什么（模型选工具主要看这个）
#   input_schema 参数结构，模型据此生成参数
# ══════════════════════════════════════════════════════════════
TOOLS = [
    {
        "name": "list_dir",
        "description": "列出指定目录下的文件和子目录，返回名称与大小。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，例如 . 表示当前目录"},
            },
            "required": ["path"],  # 模型必须给这个参数，不给就报错
        },
    }
]


def list_dir(path: str) -> str:
    """真正干活的函数。模型永远碰不到它，只有我们的代码能调。

    输入：目录路径
    输出：一段给人看也給模型看的文本（模型读到的是这段文字，不是文件对象）
    """
    p = Path(path).expanduser()  # expanduser 把 ~ 展开成用户目录
    if not p.exists():
        raise ValueError(f"路径不存在：{path}")
    if not p.is_dir():
        raise ValueError(f"不是目录：{path}")

    # 排序是为了输出稳定：同样内容每次顺序一样，模型不容易误判。
    entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
    lines = []
    # 只取前 50 条：目录太大时全塞给模型既贵又没用。
    for e in entries[:50]:
        lines.append(f"{e.name}  <dir>" if e.is_dir() else f"{e.name}  {e.stat().st_size} B")
    if len(entries) > 50:
        # 截断时必须告诉模型「我截断了」，否则它会以为只有这些。
        lines.append(f"（已截断，共 {len(entries)} 条）")
    return "\n".join(lines) if lines else "（空目录）"


def run_tool(name: str, args: dict) -> str:
    """按名字分发。任何错误都变成一段文字回给模型，绝不让程序崩。

    注意最后那个 except Exception：
    工具出错不能抛出去杀掉程序，而要变成「文字」喂回给模型，
    模型读到「路径不存在」才会自己改参数重试。这就是 agent 能自我纠错的原因。
    """
    # 分发表：名字 → 函数。以后加工具就是在这里加一行。
    registry = {"list_dir": list_dir}
    fn = registry.get(name)
    if fn is None:
        # 模型可能编出一个不存在的工具名，这里必须明确告诉它有哪些可用。
        return f"没有名为 {name} 的工具，可用：{', '.join(registry)}"
    try:
        # **args 是「按名字展开成关键字参数」：
        # args={"path":"."} 等价于 list_dir(path=".")
        return fn(**args)
    except Exception as exc:
        return f"工具执行失败：{exc}"


def call_model(messages: list, tools: list) -> dict:
    """调一次模型。这段和 V0 几乎一样，V3 起会搬到 agent/client.py。"""
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
    # 和 V0 的唯一结构差异：多带一个 tools 字段。
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
    """agent 的核心循环。V1 只有 35 行，但这是全书最重要的 35 行。"""
    # 历史的第一条永远是用户的问题。后面每轮都往这个列表里追加。
    messages = [{"role": "user", "content": question}]

    # 上限 5 轮：防止模型反复调工具不收敛。
    for step in range(MAX_ITERATIONS):
        # ── ① 问模型（每次都把完整历史发过去）────────────────
        data = call_model(messages, TOOLS)
        stop = data.get("stop_reason")

        # ── ② 把模型的回复原样记进历史 ──────────────────────
        # assistant 的 content 原样回填，包括 thinking 块和它的 signature。
        # 实测 deepseek-v4-1-flash 把 thinking 块丢掉也能继续跑，但官方要求原样带回，
        # 换模型（尤其豆包/Claude）时别赌这一点。
        messages.append({"role": "assistant", "content": data["content"]})

        # ── ③ 它想调工具 → 执行 + 回填 + 再来一轮 ────────────
        if stop == "tool_use":
            results = []
            for block in data["content"]:
                if block.get("type") != "tool_use":
                    continue  # 同一批里可能混着 text/thinking 块，跳过
                args = block.get("input") or {}
                print(f"[工具] {block['name']}({json.dumps(args, ensure_ascii=False)})")
                output = run_tool(block["name"], args)  # 这里才真正碰文件系统
                print("       " + output.replace("\n", "\n       "))
                # tool_result 靠 tool_use_id 配对，不是靠顺序。
                # 模型一次发三个工具调用时，结果必须一一对上号。
                results.append(
                    {"type": "tool_result", "tool_use_id": block["id"], "content": output}
                )
            # 所有结果放在同一条 user 消息里；模型一次要调三个工具也只需回一条。
            # 为什么是 user 角色？协议里只有 user/assistant 两种，
            # 工具结果不是模型说的，就只能算「用户侧提供的信息」。
            messages.append({"role": "user", "content": results})
            continue  # 回到循环开头，带着工具结果再问一次

        # ── ④ 它只想说话 → 打印并结束 ───────────────────────
        print(extract_text(data))
        if stop == "max_tokens":
            # 截断时要明说，否则用户会以为模型说完了。
            print("（注意：回复被 max_tokens 截断了）")

        usage = data.get("usage") or {}
        print(f"\n[用量] 输入 {usage.get('input_tokens')} / 输出 {usage.get('output_tokens')} tokens")
        return

    # 5 轮还没收敛，放弃。注意：这里没有「继续问」，是这一版的简化。
    print(f"已经转满 {MAX_ITERATIONS} 轮还没得出结论，先停下。")


if __name__ == "__main__":
    # 拼回整句：不加引号时 argv 会被空格切成好几段，全拼起来才不丢字。
    run_agent(" ".join(sys.argv[1:]) if len(sys.argv) > 1 else DEFAULT_QUESTION)
