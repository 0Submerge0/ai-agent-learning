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

    def find(self, task_id: int) -> Task:
        """按编号找任务，找不到就抛带总数的错——错误信息是给用户/模型看的。"""
        task = next((t for t in self.tasks if t.id == task_id), None)
        if task is None:
            raise ValueError(f"没有编号为 {task_id} 的任务（共 {len(self.tasks)} 个）")
        return task

    def counts(self) -> dict[str, int]:
        """按状态计数，用于收口时打「6 done / 1 failed」而不是笼统的「还有 1 个」。"""
        result = {s: 0 for s in STATUSES}
        for t in self.tasks:
            result[t.status] = result.get(t.status, 0) + 1
        return result

    def summary(self) -> str:
        c = self.counts()
        return " / ".join(f"{n} {s}" for s, n in c.items() if n)

    # 下面五个方法只有人能调（REPL 命令层），模型侧没有对应工具。

    def skip(self, task_id: int) -> Task:
        """人工跳过。已完成的跳过没有意义，直接报错。"""
        task = self.find(task_id)
        if task.status == "done":
            raise ValueError(f"任务 {task_id} 已完成，跳过没有意义")
        task.status = "skipped"
        if not task.note:
            task.note = "人工跳过"
        self.touch()
        return task

    def retry(self, task_id: int) -> Task:
        """人工重试：把任务打回 pending，并清掉上一次的失败备注。"""
        task = self.find(task_id)
        task.status = "pending"
        task.note = ""
        self.touch()
        return task

    def rename(self, task_id: int, title: str) -> Task:
        task = self.find(task_id)
        title = (title or "").strip()
        if not title:
            raise ValueError("任务标题不能为空")
        task.title = title
        self.touch()
        return task

    def add(self, title: str) -> Task:
        title = (title or "").strip()
        if not title:
            raise ValueError("任务标题不能为空")
        if len(self.tasks) >= MAX_TASKS:
            raise ValueError(f"计划最多 {MAX_TASKS} 个任务，先删掉一个再添加")
        new_id = max((t.id for t in self.tasks), default=0) + 1
        task = Task(id=new_id, title=title, status="pending", note="人工新增")
        self.tasks.append(task)
        self.touch()
        return task

    def remove(self, task_id: int) -> Task:
        """删除任务并重新编号 1..N。

        编号会变，因为人读的是序号（「第 3 个」），留着空洞只会让人困惑。
        历史对话里模型可能还在说旧编号，这是这个取舍的代价。
        """
        task = self.find(task_id)
        self.tasks.remove(task)
        for i, t in enumerate(self.tasks, start=1):
            t.id = i
        self.touch()
        return task

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
