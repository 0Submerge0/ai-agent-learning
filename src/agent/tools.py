"""工具定义 + 实现 + 分发。"""

from .sandbox import rel, safe_path

TOOLS = [
    {
        "name": "list_dir",
        "description": "列出 workspace 内某个目录的文件和子目录，返回名称与大小。默认 . 表示 workspace 根目录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对 workspace 的目录路径，例如 . 或 images"},
            },
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

WRITE_TOOLS = {"mkdir", "move_file"}


def list_dir(path: str = ".") -> str:
    p = safe_path(path, must_exist=True)
    if not p.is_dir():
        raise ValueError(f"不是目录：{path}")
    entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
    lines = []
    for e in entries[:50]:
        name = rel(e)
        lines.append(f"{name}/" if e.is_dir() else f"{name}  {e.stat().st_size} B")
    if len(entries) > 50:
        lines.append(f"（已截断，共 {len(entries)} 条）")
    return "\n".join(lines) if lines else "（空目录）"


def read_file(path: str, max_chars: int = 4000) -> str:
    p = safe_path(path, must_exist=True)
    if not p.is_file():
        raise ValueError(f"不是文件：{path}")
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n（已截断，原文共 {len(text)} 字符）"
    return text


def mkdir(path: str) -> str:
    p = safe_path(path)
    if p.exists():
        raise ValueError(f"已存在：{path}")
    p.mkdir(parents=True, exist_ok=True)
    return f"已创建目录 {rel(p)}"


def move_file(src: str, dst: str) -> str:
    s = safe_path(src, must_exist=True)
    d = safe_path(dst)
    if d.exists():
        raise ValueError(f"目标已存在，拒绝覆盖：{dst}")
    d.parent.mkdir(parents=True, exist_ok=True)
    s.rename(d)
    return f"已移动 {rel(s)} → {rel(d)}"


def describe_write(name: str, args: dict) -> str:
    if name == "mkdir":
        return f"创建目录 workspace/{args.get('path')}"
    if name == "move_file":
        return f"移动 workspace/{args.get('src')} → workspace/{args.get('dst')}"
    return name


def run_tool(name: str, args: dict, ask=None) -> str:
    """按名字分发。写工具先打印计划、要用户确认；任何错误都变成文字回给模型。"""
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
        if name in WRITE_TOOLS:
            print(f"[计划] {describe_write(name, args)}")
            ask = ask or input
            try:
                answer = ask("执行? (y/N) ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "y":
                return "用户拒绝了这次操作，未执行。"
        return fn(**args)
    except Exception as exc:
        return f"工具执行失败：{exc}"
