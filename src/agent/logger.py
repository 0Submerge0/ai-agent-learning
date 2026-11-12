"""会话日志：每个事件一行 JSON（JSONL），出问题时能事后还原现场。

════════════════════════════════════════════════════════════
【为什么 agent 特别需要日志】
普通程序出错了你看堆栈。agent 出错往往是「它做了奇怪的事」，
没有堆栈，只有一串动作。所以要把每一步都记下来：

    model_request     发出了什么
    model_response    模型回了什么（stop_reason、耗时、token）
    tool_call         要调哪个工具、什么参数
    tool_result       工具返回了什么、成功没有
    write_plan        打算写什么
    write_confirm     用户批没批
    budget_warning    预算快满了
    ...

事后用 grep 一查，就能还原「它为什么在第 7 轮选错了工具」。

【为什么是 JSONL 而不是普通日志】
JSONL = 每行一个独立 JSON 对象。
    优点：一行一条、可以直接被程序解析、还能用 grep 粗略查
    对比：如果整份是「一个 JSON 数组」，中途崩了文件就坏了（少个括号）

【一条日志长什么样】
{"ts":"2026-11-11T14:18:24.512","event":"tool_call","phase":"executing",
 "tool":"move_file","args":{"src":"备份.zip","dst":"other/备份.zip"}}

【读完这个文件你应该能回答】
1. 为什么每条都 flush 一次？（性能 vs 数据安全）
2. 为什么 fields 用 **kwargs 而不是固定参数？（每个事件字段不同）
3. 为什么日志里绝对不能放 API key？
4. __enter__ / __exit__ 是干什么的？（支持 with 语法）
════════════════════════════════════════════════════════════
"""

import json
from datetime import datetime
from pathlib import Path

# 日志目录跟着项目走，不写死盘符
LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"


class SessionLogger:
    """一次 REPL 会话对应一个日志文件，比如 logs/session-20261110-153012.log。

    名字里带时间戳，所以同一台机器多次运行不会互相覆盖。
    """

    def __init__(self, log_dir: Path = LOG_DIR):
        log_dir.mkdir(parents=True, exist_ok=True)
        # 20261110-153012：精确到秒，够区分同一分钟内的两次启动
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = log_dir / f"session-{stamp}.log"
        # "a" = 追加模式。理论上不会重名，但追加更安全。
        self._fh = self.path.open("a", encoding="utf-8")

    def log(self, event: str, **fields) -> None:
        """写一行 JSON。字段名随便传，但永远不要把 API key 放进来。

        参数形式是 log("tool_call", tool="move_file", args={...})：
        event 是位置参数，其余全部收进 fields 字典。
        这样每个事件可以带不同的字段，不用预先定义 schema。
        """
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"), "event": event}
        # update 把额外字段合并进去
        record.update(fields)
        # ensure_ascii=False：中文正常写，不转成 \uXXXX（日志要能直接读）
        # default=str：万一某个字段是 Path 之类不能序列化的对象，转成字符串
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        # 每条都 flush：即使下一步程序崩了，已经发生的事也留在磁盘上。
        # 代价是每条多一次磁盘 IO，但日志量不大，值得。
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    # 下面两个方法让这个类支持 with 语法：
    #     with SessionLogger() as logger:
    #         logger.log(...)
    #     # 离开代码块时自动 close()，即使中间抛异常
    # 当前代码里没用到（用的是 try/finally），留着备用。
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
