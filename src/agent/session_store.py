"""会话存档：把 goal / phase / plan / messages / budget 落盘，供 /resume 恢复。"""

import json
from datetime import datetime
from pathlib import Path

SESSIONS_DIR = Path(__file__).resolve().parent.parent.parent / "sessions"


class SessionStore:
    """一个 REPL 进程对应一个存档文件，但只有真正保存过才落盘。"""

    def __init__(self, session_id: str | None = None, sessions_dir: Path = SESSIONS_DIR):
        self.dir = Path(sessions_dir)
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"session-{self.session_id}.json"

    def adopt(self, path: Path) -> None:
        """接管一个已有存档，后续保存写回同一个文件。"""
        self.path = Path(path)
        self.dir = self.path.parent
        self.session_id = self.path.stem.removeprefix("session-")

    def save(self, *, goal: str, phase: str, plan, messages: list, budget) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": self.session_id,
            "goal": goal,
            "phase": phase,
            "plan": plan.to_dict() if plan is not None else None,
            "messages": messages,
            "budget": budget.to_dict(),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 先写临时文件再替换：中途崩了也不会留下半个 JSON。
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    @staticmethod
    def latest(sessions_dir: Path = SESSIONS_DIR) -> Path | None:
        root = Path(sessions_dir)
        if not root.exists():
            return None
        files = sorted(root.glob("session-*.json"), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None

    @staticmethod
    def load(path: Path) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))
