"""会话预算：按 token 计数，超限就停，避免一次任务烧掉整月额度。"""

import os

DEFAULT_MAX_TOKENS = 20000
WARN_RATIO = 0.8


class Budget:
    def __init__(self, max_tokens: int | None = None):
        if max_tokens is None:
            raw = (os.environ.get("MAX_SESSION_TOKENS") or "").strip()
            try:
                max_tokens = int(raw) if raw else DEFAULT_MAX_TOKENS
            except ValueError:
                # 环境变量写错不该让程序起不来，退回默认值。
                max_tokens = DEFAULT_MAX_TOKENS
        self.max_tokens = max_tokens
        self.input_tokens = 0
        self.output_tokens = 0
        self._warned = False

    @property
    def used(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def remaining(self) -> int:
        return max(0, self.max_tokens - self.used)

    def add(self, usage: dict) -> str:
        """累加一次调用的用量，返回 ok / warning / exhausted。

        warning 只在第一次越过 80% 时返回一次，避免每轮都提醒。
        """
        self.input_tokens += int((usage or {}).get("input_tokens") or 0)
        self.output_tokens += int((usage or {}).get("output_tokens") or 0)

        if self.used >= self.max_tokens:
            return "exhausted"
        if not self._warned and self.used >= self.max_tokens * WARN_RATIO:
            self._warned = True
            return "warning"
        return "ok"

    def reset(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self._warned = False

    def status(self) -> str:
        return f"{self.used}/{self.max_tokens} tokens（剩 {self.remaining}）"
