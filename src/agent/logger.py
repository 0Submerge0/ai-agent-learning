"""会话日志：每个事件一行 JSON（JSONL），出问题时能事后还原现场。"""

import json
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"


class SessionLogger:
    """一次 REPL 会话对应一个日志文件，比如 logs/session-20261110-153012.log。"""

    def __init__(self, log_dir: Path = LOG_DIR):
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = log_dir / f"session-{stamp}.log"
        self._fh = self.path.open("a", encoding="utf-8")

    def log(self, event: str, **fields) -> None:
        """写一行 JSON。字段名随便传，但永远不要把 API key 放进来。"""
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"), "event": event}
        record.update(fields)
        self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        # 每条都 flush：即使下一步程序崩了，已经发生的事也留在磁盘上。
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
