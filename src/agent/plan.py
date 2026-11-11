"""显式任务计划：把「计划」从提示词文本变成可检查、可保存的状态。"""

from dataclasses import dataclass, field
from datetime import datetime

STATUSES = ("pending", "in_progress", "done", "failed", "skipped")
MAX_TASKS = 12


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Task:
    id: int
    title: str
    status: str = "pending"
    note: str = ""


@dataclass
class Plan:
    goal: str
    tasks: list[Task] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def touch(self) -> None:
        self.updated_at = _now()

    def set_tasks(self, titles: list[str]) -> None:
        """用一组任务标题重建计划。模型通过 create_plan 调用。"""
        if not isinstance(titles, list):
            raise ValueError("tasks 必须是字符串数组")
        clean = []
        for title in titles:
            if not isinstance(title, str):
                raise ValueError("每个任务都必须是字符串")
            title = title.strip()
            if title:
                clean.append(title)
        if not clean:
            raise ValueError("计划至少需要一个任务")
        if len(clean) > MAX_TASKS:
            raise ValueError(f"计划最多 {MAX_TASKS} 个任务，收到 {len(clean)} 个")

        self.tasks = [Task(id=i + 1, title=t) for i, t in enumerate(clean)]
        self.touch()

    def update(self, task_id: int, status: str, note: str = "") -> Task:
        """更新一个任务的状态。模型通过 update_task 调用。"""
        if status not in STATUSES:
            raise ValueError(f"未知状态：{status}，可用：{', '.join(STATUSES)}")
        task = next((t for t in self.tasks if t.id == task_id), None)
        if task is None:
            raise ValueError(f"没有编号为 {task_id} 的任务（共 {len(self.tasks)} 个）")

        task.status = status
        if note is not None:
            task.note = str(note).strip()[:200]
        self.touch()
        return task

    @property
    def done_count(self) -> int:
        return sum(1 for t in self.tasks if t.status in ("done", "skipped"))

    @property
    def is_complete(self) -> bool:
        return bool(self.tasks) and all(t.status in ("done", "skipped") for t in self.tasks)

    def progress_text(self) -> str:
        return f"{self.done_count}/{len(self.tasks)} 完成"

    def render(self) -> str:
        lines = [f"目标：{self.goal}", "任务："]
        for t in self.tasks:
            line = f"  {t.id}. [{t.status}] {t.title}"
            if t.note:
                line += f"（{t.note}）"
            lines.append(line)
        lines.append(f"进度：{self.progress_text()}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "tasks": [
                {"id": t.id, "title": t.title, "status": t.status, "note": t.note}
                for t in self.tasks
            ],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Plan":
        data = data or {}
        tasks = [
            Task(
                id=int(t.get("id", i + 1)),
                title=str(t.get("title", "")),
                status=str(t.get("status", "pending")),
                note=str(t.get("note", "")),
            )
            for i, t in enumerate(data.get("tasks") or [])
        ]
        return cls(
            goal=str(data.get("goal", "")),
            tasks=tasks,
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or _now()),
        )
