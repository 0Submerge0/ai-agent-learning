"""显式任务计划：把「计划」从提示词文本变成可检查、可保存的状态。

════════════════════════════════════════════════════════════
【这个文件是 V5 的核心，也是整个项目「质变」的地方】
V4 的计划是模型嘴里的一句话：

    「我打算：1.建目录 2.移图片 3.写报告」

这句话有什么问题？
    - 代码不知道做到第几步了（只能靠模型自己说）
    - 模型可以改口，你没法发现
    - 中断之后没法恢复（那只是历史里的一段文本）
    - 「完成了没有」只能靠模型宣布

V5 把计划变成一个**对象**：
    Plan(goal="整理 workspace", tasks=[Task(1,"建目录"), Task(2,"移图片")])

变成对象之后，上面四件事全部变成可计算的：
    plan.is_complete     代码算的，不是模型说的
    plan.progress_text() 从任务状态算出「3/7 完成」
    plan.to_dict()       可以存成 JSON，下次读回来
    plan.render()        拍成文本，塞进提示词给模型看

【就两个 dataclass，没有魔法】
    Task  一个任务：编号、标题、状态、备注
    Plan  一个计划：目标 + 一串 Task + 时间戳

所有复杂度都在「状态机」上：5 个状态，谁能变成谁。

【5 个状态，注意 failed 和 skipped 的区别】
    pending      还没做
    in_progress  正在做（这个状态最危险：中断时停在这里，模型下次会以为还在做）
    done         做完了
    failed       试过了，没成
    skipped      不用做（人工跳过）

    「完成」的定义是 done 或 skipped —— 见下面的 is_complete。
    failed 不算完成。这就是为什么模型把任务标成 failed 之后，
    计划永远显示「未收口」。

【这个文件有两类方法，别混】
    上半部分（set_tasks / update）  ← 模型通过工具调用
    下半部分（skip / retry / rename / add / remove） ← 只有人能调（V6 的 REPL）
分界线就是下面那句「V6 加的方法：只有人能调」注释。
════════════════════════════════════════════════════════════
"""

from dataclasses import dataclass, field
from datetime import datetime

# 状态闭集。改这个元组 = 改状态机，所有校验都依赖它。
STATUSES = ("pending", "in_progress", "done", "failed", "skipped")

# 上限 12。为什么要有上限？
# 计划越长，模型越容易漏掉中间的任务，提示词也越贵。
MAX_TASKS = 12


def _now() -> str:
    """统一的时间格式：2026-11-11T14:18:24。

    抽成函数是因为 dataclass 的 default_factory 需要「一个可调用的对象」，
    而且保证创建时间和更新时间格式一致。
    """
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Task:
    """一个任务。

    @dataclass 会自动生成 __init__、__repr__、__eq__。
    所以 Task(id=1, title="a") 直接就能用，打印出来是
    Task(id=1, title='a', status='pending', note='')。
    """
    id: int
    title: str
    status: str = "pending"  # 默认值，所以只传 id 和 title 也行
    note: str = ""           # 失败原因、跳过原因之类的补充说明


@dataclass
class Plan:
    """一个计划。

    field(default_factory=list) 而不是直接写 []：
    Python 的可变默认值陷阱——写成 tasks: list = [] 的话，
    所有 Plan 实例会共享同一个列表。default_factory 每次创建新的。
    """
    goal: str
    tasks: list[Task] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def touch(self) -> None:
        """更新 updated_at。任何改动之后都要调用。"""
        self.updated_at = _now()

    # ══════════════════════════════════════════════════════════
    # 模型能调的两个方法
    # ══════════════════════════════════════════════════════════

    def set_tasks(self, titles: list[str]) -> None:
        """用一组任务标题重建计划。模型通过 create_plan 调用。

        注意是「重建」：每次都整体替换，不是追加。
        因为模型提交的是完整计划，重复调用应该覆盖而不是叠加。

        这一串校验不是为了防恶意，是为了给模型清晰的错误信息——
        它读到「计划最多 12 个任务，收到 15 个」就会自己缩减。
        """
        if not isinstance(titles, list):
            raise ValueError("tasks 必须是字符串数组")
        clean = []
        for title in titles:
            if not isinstance(title, str):
                raise ValueError("每个任务都必须是字符串")
            title = title.strip()
            if title:  # 空标题直接丢掉，不报错——这是常见的无害噪音
                clean.append(title)
        if not clean:
            raise ValueError("计划至少需要一个任务")
        if len(clean) > MAX_TASKS:
            raise ValueError(f"计划最多 {MAX_TASKS} 个任务，收到 {len(clean)} 个")

        # enumerate(clean) 给出 (0, "建目录")、(1, "移图片")…
        # 所以 id 从 i+1 开始，是 1-based，方便人读「第 3 个任务」
        self.tasks = [Task(id=i + 1, title=t) for i, t in enumerate(clean)]
        self.touch()

    def update(self, task_id: int, status: str, note: str = "") -> Task:
        """更新一个任务的状态。模型通过 update_task 调用。

        这是模型唯一能改计划的方式：改状态，不能增删改任务本身。
        """
        if status not in STATUSES:
            # 模型可能编一个 "completed" 出来，这里必须拦住并告诉它合法值
            raise ValueError(f"未知状态：{status}，可用：{', '.join(STATUSES)}")
        # next((...), None)：找到第一个满足的，找不到返回 None
        task = next((t for t in self.tasks if t.id == task_id), None)
        if task is None:
            raise ValueError(f"没有编号为 {task_id} 的任务（共 {len(self.tasks)} 个）")

        task.status = status
        # note 限制 200 字符：模型有时会写一大段，会污染计划渲染
        if note is not None:
            task.note = str(note).strip()[:200]
        self.touch()
        return task

    # ══════════════════════════════════════════════════════════
    # 派生的只读信息（都是算出来的，不存储）
    # ══════════════════════════════════════════════════════════

    @property
    def done_count(self) -> int:
        # 注意：skipped 也算「完成」。
        # 这是 V5 就定下的规则，V6 的 /skip 正是利用它来推进进度。
        return sum(1 for t in self.tasks if t.status in ("done", "skipped"))

    @property
    def is_complete(self) -> bool:
        """全部 done 或 skipped 才算完成——failed 不算。

        为什么 failed 不算？
        因为 failed 意味着「这件事没做成」。计划的目标没达到，
        就还不能收工。V5 的坑就是：模型一旦标 failed，计划永远收不了口。
        """
        return bool(self.tasks) and all(t.status in ("done", "skipped") for t in self.tasks)

    def progress_text(self) -> str:
        """「3/7 完成」这样的一行。"""
        return f"{self.done_count}/{len(self.tasks)} 完成"

    def render(self) -> str:
        """拍成给人看（也给模型看）的文本。

        这个方法每个版本都在用：
          V5 塞进 system prompt      → 模型每轮都看到最新计划
          V6 的 /plan 命令            → 用户看进度
          V6 的 takeover_message      → 接管时把计划重新告诉模型
        所以格式要稳定、紧凑。
        """
        lines = [f"目标：{self.goal}", "任务："]
        for t in self.tasks:
            line = f"  {t.id}. [{t.status}] {t.title}"
            if t.note:
                line += f"（{t.note}）"  # 备注跟在后面，一眼看到「为什么失败」
            lines.append(line)
        lines.append(f"进度：{self.progress_text()}")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════
    # V6 加的方法：只有人能调（REPL 命令层），模型侧没有对应工具
    #
    # 为什么不分给模型？因为计划是用户批准的。
    # 详见 docs/notes/v6.md §2.2。
    # ══════════════════════════════════════════════════════════

    def find(self, task_id: int) -> Task:
        """按编号找任务，找不到就抛带总数的错——错误信息是给用户/模型看的。

        抽这个方法是因为下面五个操作都要先找任务。
        错误信息里带「共 N 个」，是因为模型常引用已删除的旧编号，
        看到总数它就能自我修正。
        """
        task = next((t for t in self.tasks if t.id == task_id), None)
        if task is None:
            raise ValueError(f"没有编号为 {task_id} 的任务（共 {len(self.tasks)} 个）")
        return task

    def counts(self) -> dict[str, int]:
        """按状态计数，用于收口时打「6 done / 1 failed」而不是笼统的「还有 1 个」。

        返回的是完整 5 个键，没有的就是 0。
        这样调用方写 c["failed"] 永远不会 KeyError。
        """
        result = {s: 0 for s in STATUSES}
        for t in self.tasks:
            result[t.status] = result.get(t.status, 0) + 1
        return result

    def summary(self) -> str:
        """「4 pending / 2 done / 1 skipped」这样的一行。只列非零项。"""
        c = self.counts()
        return " / ".join(f"{n} {s}" for s, n in c.items() if n)

    def skip(self, task_id: int) -> Task:
        """人工跳过。已完成的跳过没有意义，直接报错。"""
        task = self.find(task_id)
        if task.status == "done":
            raise ValueError(f"任务 {task_id} 已完成，跳过没有意义")
        task.status = "skipped"
        # 留一句备注，否则看计划时不知道这是「本来就不用做」还是「临时跳过」
        if not task.note:
            task.note = "人工跳过"
        self.touch()
        return task

    def retry(self, task_id: int) -> Task:
        """人工重试：把任务打回 pending，并清掉上一次的失败备注。

        为什么清 note？
        那条备注写的是「上次为什么失败」。任务重做了还留着，
        模型（和用户）会以为这次也失败了。
        """
        task = self.find(task_id)
        task.status = "pending"
        task.note = ""
        self.touch()
        return task

    def rename(self, task_id: int, title: str) -> Task:
        """改任务描述，不动状态。"""
        task = self.find(task_id)
        title = (title or "").strip()
        if not title:
            raise ValueError("任务标题不能为空")
        task.title = title
        self.touch()
        return task

    def add(self, title: str) -> Task:
        """末尾追加一个任务。新编号 = 当前最大编号 + 1。"""
        title = (title or "").strip()
        if not title:
            raise ValueError("任务标题不能为空")
        if len(self.tasks) >= MAX_TASKS:
            raise ValueError(f"计划最多 {MAX_TASKS} 个任务，先删掉一个再添加")
        # 用「最大 id + 1」而不是 len+1：这样即使中间删过任务也不会撞号。
        # default=0 处理空计划的情况。
        new_id = max((t.id for t in self.tasks), default=0) + 1
        task = Task(id=new_id, title=title, status="pending", note="人工新增")
        self.tasks.append(task)
        self.touch()
        return task

    def remove(self, task_id: int) -> Task:
        """删除任务并重新编号 1..N。

        编号会变，因为人读的是序号（「第 3 个」），留着空洞只会让人困惑。
        历史对话里模型可能还在说旧编号，这是这个取舍的代价。

        另一种做法是保留空洞（删了 3 就永远没有 3）：
        好处是编号稳定、模型引用不会错，坏处是人看着别扭。
        这里选了重编号。
        """
        task = self.find(task_id)
        self.tasks.remove(task)
        # 重新编号：enumerate(..., start=1) 给 1,2,3...
        for i, t in enumerate(self.tasks, start=1):
            t.id = i
        self.touch()
        return task

    # ══════════════════════════════════════════════════════════
    # 序列化：存档 / 读档
    # ══════════════════════════════════════════════════════════

    def to_dict(self) -> dict:
        """变成能写进 JSON 的普通 dict。

        dataclass 对象不能直接被 json.dumps 序列化，
        所以要把每个 Task 手工摊平成 dict。
        """
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
        """从存档 dict 还原成 Plan 对象。

        所有字段都用了 get(默认值)：因为存档可能是旧版本写的，
        缺字段时要能读出来，而不是崩掉。

        @classmethod 的第一个参数是类本身（cls），不是实例（self），
        所以可以用 cls(...) 创建对象。这就是「工厂方法」。
        """
        data = data or {}
        tasks = [
            Task(
                # 存档里没 id 就用序号兜底
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
            # 时间是字符串，缺了就用当前时间
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or _now()),
        )
