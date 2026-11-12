"""会话预算：按 token 计数，超限就停，避免一次任务烧掉整月额度。

════════════════════════════════════════════════════════════
【为什么需要这个】
agent 的费用和聊天不一样：它一轮对话里可能连着调 5 次模型，
每次都要把完整历史重新发一遍。所以花钱速度是「对话轮数 × 历史长度」，
不盯着很容易一次任务就烧掉几万 token。

【两个数字的区别，别混】
    max_tokens    模型的「单次输出上限」（在 client.py 里，通常是 1024）
    max_tokens    本类的「整个会话总预算」（默认 100000）
同名但是两回事。

【它是一根保险丝，不是配额】
默认值可以随便调（环境变量 MAX_SESSION_TOKENS）。
重要的不是数字多大，而是「有没有检查点」——
V5 实测过：检查发生在每轮开头，一轮就可能冲爆 4 万预算。

【读完这个文件你应该能回答】
1. used / remaining 为什么做成 @property 而不是普通字段？
2. add() 为什么返回字符串 "ok"/"warning"/"exhausted"，而不是抛异常？
3. restore() 为什么取 max(存档值, 环境变量值)？
4. 环境变量写错（比如 "abc"）时为什么不让程序崩？
════════════════════════════════════════════════════════════
"""

import os

# 默认总预算。这个值被改过好几次，说明它就是个可调的旋钮。
DEFAULT_MAX_TOKENS = 100000

# 到 80% 时提醒一次。为什么不是 100%？因为到 100% 时已经停了，提醒没意义。
WARN_RATIO = 0.8


class Budget:
    """会话预算计数器。"""

    def __init__(self, max_tokens: int | None = None):
        # 参数优先，其次环境变量，最后默认值。
        if max_tokens is None:
            raw = (os.environ.get("MAX_SESSION_TOKENS") or "").strip()
            try:
                max_tokens = int(raw) if raw else DEFAULT_MAX_TOKENS
            except ValueError:
                # 环境变量写错不该让程序起不来，退回默认值。
                # 这类「配置错误不该炸」的思路在工具链里很常见。
                max_tokens = DEFAULT_MAX_TOKENS

        self.max_tokens = max_tokens
        # 分开记输入和输出：报告用量时两个都要，价格也可能不同。
        self.input_tokens = 0
        self.output_tokens = 0
        # 只在第一次越过 80% 时提醒，避免每轮都刷屏
        self._warned = False

    # @property 让方法可以像字段一样访问：budget.used，不写括号。
    # 好处是「值是算出来的」，不会忘了更新。
    @property
    def used(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def remaining(self) -> int:
        # max(0, ...) 防止冲爆后出现负数，打印出来会很奇怪
        return max(0, self.max_tokens - self.used)

    def add(self, usage: dict) -> str:
        """累加一次调用的用量，返回 ok / warning / exhausted。

        warning 只在第一次越过 80% 时返回一次，避免每轮都提醒。

        为什么用字符串返回值而不是抛异常？
        因为「预算用完」不是程序错误，是正常业务流程——
        调用方要根据它做不同的事（提示、保存、退出），不是崩掉。
        """
        # usage 来自响应里的 usage 字段。用 or 0 兜住字段缺失的情况。
        self.input_tokens += int((usage or {}).get("input_tokens") or 0)
        self.output_tokens += int((usage or {}).get("output_tokens") or 0)

        if self.used >= self.max_tokens:
            return "exhausted"
        if not self._warned and self.used >= self.max_tokens * WARN_RATIO:
            self._warned = True
            return "warning"
        return "ok"

    def reset(self) -> None:
        """/clear 时调用：计数归零，提醒标记也复位。"""
        self.input_tokens = 0
        self.output_tokens = 0
        self._warned = False

    def status(self) -> str:
        """给终端看的一行摘要。"""
        return f"{self.used}/{self.max_tokens} tokens（剩 {self.remaining}）"

    def to_dict(self) -> dict:
        """存档用。不带 _warned，因为那是运行期状态，恢复时重新算。"""
        return {
            "max_tokens": self.max_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    def restore(self, data: dict | None) -> None:
        """从存档恢复用量。

        用量一定照搬（否则恢复等于偷额度）；上限取「存档值」和「本次环境变量值」
        中较大的那个——允许你把 MAX_SESSION_TOKENS 调大继续跑，但不允许悄悄调小。

        为什么要防「悄悄调小」？
        否则你可以保存一个用了 8 万的会话，然后把环境变量设成 1000，
        恢复后看起来只用了 8 千——预算形同虚设。
        """
        data = data or {}
        stored_max = int(data.get("max_tokens") or 0)
        # max 的结果：调大生效，调小无效
        self.max_tokens = max(stored_max, self.max_tokens)
        self.input_tokens = int(data.get("input_tokens") or 0)
        self.output_tokens = int(data.get("output_tokens") or 0)
        # 恢复后如果已经超过 80%，直接把提醒标记设上，避免立刻又提醒一次
        self._warned = self.used >= self.max_tokens * WARN_RATIO
