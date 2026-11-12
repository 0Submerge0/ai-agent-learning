"""会话存档：把 goal / phase / plan / messages / budget 落盘，供 /resume 恢复。

════════════════════════════════════════════════════════════
【为什么「恢复」能成立】
因为模型是无状态的——服务端什么都不存。
所以只要把这四样东西原样存下来、原样发回去，
模型看到的历史和退出前一模一样，自然能接着做：

    goal      用户的原话（目标）
    phase     阶段：planning / awaiting_approval / executing / done / rejected
    plan      计划 + 每个任务的状态
    messages  完整对话历史（这是最大的一块）
    budget    花了多少 token（恢复时不能白拿额度）

【phase 是干什么的】
它决定 /resume 之后能做什么：
    awaiting_approval  问你「批准这个计划吗」
    executing          直接接着跑
    done               说「已经完成，不需要继续」
没有它的话，恢复后不知道停在哪一步。

【这个文件最关键的一行：save() 里的 tmp.replace()】
原子写。先写 .tmp，写完再替换目标文件。
直接写目标文件的话，写到一半断电 → 半个 JSON → 下次 /resume 直接解析失败，
整个会话作废。这种「写一半」在长任务里概率不低，因为存档很频繁。

【读完这个文件你应该能回答】
1. 为什么存档要原子写？不原子写最坏会发生什么？
2. latest() 按 mtime 排序取最后一个，为什么不按文件名排？
3. adopt() 是干什么用的？（恢复后保存要写回同一个文件，不是新建一个）
4. 为什么 messages 也存？它可能有几百 KB。
════════════════════════════════════════════════════════════
"""

import json
from datetime import datetime
from pathlib import Path

# 存档目录。和 logs/ 一样跟着项目走。
SESSIONS_DIR = Path(__file__).resolve().parent.parent.parent / "sessions"


class SessionStore:
    """一个 REPL 进程对应一个存档文件，但只有真正保存过才落盘。

    注意「只有真正保存过才落盘」：
    构造函数只是算了个路径，并没有创建文件。
    所以「打开程序看了一眼就退出」不会留下一堆空存档。
    """

    def __init__(self, session_id: str | None = None, sessions_dir: Path = SESSIONS_DIR):
        self.dir = Path(sessions_dir)
        # 默认用当前时间做 id，所以每次启动都是新存档；
        # 传 session_id 是为了测试时能固定文件名。
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"session-{self.session_id}.json"

    def adopt(self, path: Path) -> None:
        """接管一个已有存档，后续保存写回同一个文件。

        没有这个方法的话，/resume 之后的 save 会新建一个文件，
        于是同一段任务被拆成好几个存档，latest() 取到的可能是旧的那个。
        """
        self.path = Path(path)
        self.dir = self.path.parent
        # removeprefix 是 Python 3.9+：把 "session-20261111-150000" 还原成 "20261111-150000"
        self.session_id = self.path.stem.removeprefix("session-")

    def save(self, *, goal: str, phase: str, plan, messages: list, budget) -> None:
        """保存一次快照。

        参数列表开头的 * 表示「后面所有参数必须写成关键字形式」：
            store.save(goal=..., phase=...)  ✅
            store.save("目标", "executing", ...)  ❌
        五个参数的顺序很容易记错，强制写名字能避免调反。
        """
        self.dir.mkdir(parents=True, exist_ok=True)

        # ── 组装要存的全部现场 ──────────────────────────────
        payload = {
            "session_id": self.session_id,
            "goal": goal,
            "phase": phase,
            # plan 可能是 None（比如计划被拒绝时），这时存 null
            "plan": plan.to_dict() if plan is not None else None,
            # messages 就是模型看到的历史，原样存
            "messages": messages,
            "budget": budget.to_dict(),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

        # ── 原子写 ──────────────────────────────────────────
        # 先写临时文件再替换：中途崩了也不会留下半个 JSON。
        # indent=2 是为了人也能读（调试时直接打开看）
        # ensure_ascii=False 让中文正常显示，不转成 \uXXXX
        # default=str 兜住 Path 之类不能序列化的对象
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        # replace 在同一分区上是原子操作：
        # 要么还是旧文件，要么已经是完整的新文件，不存在中间态。
        tmp.replace(self.path)

    @staticmethod
    def latest(sessions_dir: Path = SESSIONS_DIR) -> Path | None:
        """找最近一次存档。

        按 mtime（修改时间）排序而不是文件名：
        因为 adopt 之后会覆写同一个文件，文件名的时间戳可能比内容旧。
        mtime 才是「最后动过的时间」。
        """
        root = Path(sessions_dir)
        if not root.exists():
            return None
        files = sorted(root.glob("session-*.json"), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None

    @staticmethod
    def load(path: Path) -> dict:
        """读存档。故意不做 try/except——读失败让调用方处理，
        因为「存档坏了」和「没有存档」需要给用户不同的提示。"""
        return json.loads(Path(path).read_text(encoding="utf-8"))
