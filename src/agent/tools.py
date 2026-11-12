"""V3 工具集：4 个文件工具的定义 + 实现 + 分发。

════════════════════════════════════════════════════════════
【这个文件的结构：三件套，反复出现】
    ① TOOLS      给模型看的「说明书」（JSON Schema）
    ② 各函数实现  真正干活的部分
    ③ run_tool   按名字分发 + 写操作确认闸门

以后你要加一个工具，就是在这三个地方各改一处。
漏掉任何一处，模型都会「调了没反应」或者「提示词是空的」。

【两个分组，别搞混】
    TOOLS        全部 4 个工具（读 + 写）
    WRITE_TOOLS  其中属于「写」的 2 个（mkdir / move_file）
    只有 WRITE_TOOLS 里的才会弹 y/N 确认。

【读完这个文件你应该能回答】
1. 模型的 input_schema 和函数的真实签名是什么关系？（只是约定，不强校验）
2. 为什么 run_tool 里所有异常都被吃掉变成字符串？
3. `ask = ask or input` 这行是干什么用的？（让测试可以注入假的输入函数）
4. describe_write 为什么单独存在，不能直接在 run_tool 里拼字符串？
════════════════════════════════════════════════════════════
"""

from .sandbox import rel, safe_path

# ══════════════════════════════════════════════════════════════
# ① 工具说明书
# ══════════════════════════════════════════════════════════════
TOOLS = [
    {
        "name": "list_dir",
        "description": "列出 workspace 内某个目录的文件和子目录，返回名称与大小。默认 . 表示 workspace 根目录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对 workspace 的目录路径，例如 . 或 images"},
            },
            # 空数组表示没有必填参数：模型不传 path，函数用默认值 "."
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": "读取 workspace 内一个文本文件的内容，最多返回 4000 个字符，超出会截断。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对 workspace 的文件路径"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "mkdir",
        "description": "在 workspace 内创建一个目录（父目录会一并创建）。这是写操作，需要用户确认。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对 workspace 的目录路径"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "move_file",
        "description": "在 workspace 内移动或重命名文件/目录。目标已存在时拒绝覆盖。这是写操作，需要用户确认。",
        "input_schema": {
            "type": "object",
            "properties": {
                "src": {"type": "string", "description": "源路径（相对 workspace）"},
                "dst": {"type": "string", "description": "目标路径（相对 workspace）"},
            },
            "required": ["src", "dst"],
        },
    },
]

# 写操作名单。加新工具时如果它会改磁盘，必须加进来，否则会绕过确认闸门。
WRITE_TOOLS = {"mkdir", "move_file"}


# ══════════════════════════════════════════════════════════════
# ② 实现
# ══════════════════════════════════════════════════════════════

def list_dir(path: str = ".") -> str:
    """列目录。只读，不需要确认。"""
    # must_exist=True：要列一个不存在的目录，直接报错比返回空字符串更好——
    # 模型会读到「路径不存在」，然后自己改成正确的路径。
    p = safe_path(path, must_exist=True)
    if not p.is_dir():
        raise ValueError(f"不是目录：{path}")

    entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
    lines = []
    for e in entries[:50]:
        name = rel(e)  # 显示成相对路径，短且省 token
        # 目录末尾加 /，让模型一眼看出这是目录不是文件
        lines.append(f"{name}/" if e.is_dir() else f"{name}  {e.stat().st_size} B")
    if len(entries) > 50:
        # 截断必须明说，否则模型会以为目录里只有 50 个东西
        lines.append(f"（已截断，共 {len(entries)} 条）")
    return "\n".join(lines) if lines else "（空目录）"


def read_file(path: str, max_chars: int = 4000) -> str:
    """读文件。只读，不需要确认。

    为什么要截断？
    一个 100KB 的文件塞进上下文要花几万 token，而且大部分内容对当前任务无用。
    4000 字符是个经验值：够看清结构，又不至于太贵。
    """
    p = safe_path(path, must_exist=True)
    if not p.is_file():
        raise ValueError(f"不是文件：{path}")
    # errors="replace"：二进制文件被误读时不崩，坏字符变成替换符
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        # 同样要告诉模型「我截断了」，否则它会以为文件就这么长
        return text[:max_chars] + f"\n（已截断，原文共 {len(text)} 字符）"
    return text


def mkdir(path: str) -> str:
    """建目录。写操作，会走确认闸门。"""
    # must_exist=False（默认）：目标本来就不该存在
    p = safe_path(path)
    if p.exists():
        raise ValueError(f"已存在：{path}")
    # parents=True：一次建多层；exist_ok=True：并发时避免竞态报错
    p.mkdir(parents=True, exist_ok=True)
    return f"已创建目录 {rel(p)}"


def move_file(src: str, dst: str) -> str:
    """移动或重命名。写操作，会走确认闸门。

    注意：这里用的是 rename，同一个磁盘分区内是原子操作。
    """
    s = safe_path(src, must_exist=True)  # 源必须存在
    d = safe_path(dst)                   # 目标可以不存在
    if d.exists():
        # 拒绝覆盖——这是有意的保守设计。
        # 覆盖是不可逆的，而「移动失败」只是多一步，用户能重来。
        raise ValueError(f"目标已存在，拒绝覆盖：{dst}")
    # 目标可能在很深的新目录里，先把父目录建出来
    d.parent.mkdir(parents=True, exist_ok=True)
    s.rename(d)
    return f"已移动 {rel(s)} → {rel(d)}"


# ══════════════════════════════════════════════════════════════
# ③ 分发 + 确认闸门
# ══════════════════════════════════════════════════════════════

def describe_write(name: str, args: dict) -> str:
    """把「要做什么」翻译成一句人话，给用户看。

    为什么不直接在 run_tool 里拼字符串？
    因为 V4 要扩展它（write_file 需要显示内容预览），
    抽成独立函数后 V4 可以只覆写自己关心的分支。
    """
    if name == "mkdir":
        return f"创建目录 workspace/{args.get('path')}"
    if name == "move_file":
        return f"移动 workspace/{args.get('src')} → workspace/{args.get('dst')}"
    return name  # 兜底：没写描述就用工具名


def run_tool(name: str, args: dict, ask=None) -> str:
    """按名字分发。写工具先打印计划、要用户确认；任何错误都变成文字回给模型。

    参数 ask 是为了可测试：默认用内置 input，
    测试时可以传一个假的函数，自动回答 "y" 或 "n"。
    """
    registry = {
        "list_dir": list_dir,
        "read_file": read_file,
        "mkdir": mkdir,
        "move_file": move_file,
    }
    fn = registry.get(name)
    if fn is None:
        return f"没有名为 {name} 的工具，可用：{', '.join(registry)}"
    try:
        # ── 确认闸门：只有写操作才走这一段 ──────────────────
        if name in WRITE_TOOLS:
            # 先把「要做什么」打印出来，用户才知道自己在批什么。
            print(f"[计划] {describe_write(name, args)}")
            ask = ask or input  # 没传就用真的 input()
            try:
                answer = ask("执行? (y/N) ")
            except EOFError:
                # 管道输入结束（比如 echo 喂完脚本了）当成拒绝，而不是崩掉
                answer = ""
            # 只有明确输入 y 才执行；回车、n、其他任何东西都算拒绝。
            # 默认值是「不执行」，这是安全设计里最重要的一条。
            if answer.strip().lower() != "y":
                return "用户拒绝了这次操作，未执行。"
        # ── 真正执行 ────────────────────────────────────────
        return fn(**args)
    except Exception as exc:
        # 注意：这里连确认闸门的异常也一起兜住。
        # 任何失败都变成字符串回给模型，程序本身不崩。
        return f"工具执行失败：{exc}"
