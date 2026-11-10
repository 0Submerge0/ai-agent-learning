"""V4 工具集：V3 的 4 个工具 + search_files + write_file，带日志钩子。"""

from pathlib import Path

from .sandbox import WORKSPACE, rel, safe_path
from .tools import TOOLS as TOOLS_V3
from .tools import WRITE_TOOLS as WRITE_TOOLS_V3
from .tools import describe_write, list_dir, mkdir, move_file, read_file

TOOLS_V4 = TOOLS_V3 + [
    {
        "name": "search_files",
        "description": "在 workspace 内按文件名模式递归搜索，例如 *.jpg 或 发票*。只读。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "文件名模式，如 *.jpg、*.txt、报销*"},
                "path": {"type": "string", "description": "从哪个目录开始搜，默认 . 表示 workspace 根目录"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "write_file",
        "description": "在 workspace 内写入一个 UTF-8 文本文件，父目录会自动创建。目标已存在时必须把 overwrite 设为 true。这是写操作，需要用户确认。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对 workspace 的文件路径"},
                "content": {"type": "string", "description": "要写入的完整文本内容"},
                "overwrite": {"type": "boolean", "description": "目标已存在时是否允许覆盖，默认 false"},
            },
            "required": ["path", "content"],
        },
    },
]

WRITE_TOOLS = WRITE_TOOLS_V3 | {"write_file"}


def summarize_args(name: str, args: dict) -> dict:
    """日志里只记参数的关键部分：write_file 的内容可能很长，也可能含敏感信息。"""
    if name == "write_file":
        return {
            "path": args.get("path"),
            "chars": len(args.get("content") or ""),
            "overwrite": bool(args.get("overwrite")),
        }
    return dict(args)


def search_files(pattern: str, path: str = ".") -> str:
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("搜索模式不能为空")
    # 模式只允许是「文件名模式」，不能借它跳出 workspace。
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        raise ValueError(f"搜索模式不能是绝对路径或包含 ..：{pattern}")

    base = safe_path(path, must_exist=True)
    if not base.is_dir():
        raise ValueError(f"不是目录：{path}")

    root = WORKSPACE.resolve()
    hits = []
    for entry in base.rglob(pattern):
        target = entry.resolve()
        # 符号链接可能指向 workspace 外面，逐个再验一次。
        if not target.is_relative_to(root):
            continue
        hits.append(entry)
        if len(hits) > 50:
            break

    hits.sort(key=lambda e: rel(e).lower())
    lines = []
    for entry in hits[:50]:
        lines.append(f"{rel(entry)}/" if entry.is_dir() else f"{rel(entry)}  {entry.stat().st_size} B")
    if len(hits) > 50:
        lines.append("（已截断，只显示前 50 条）")
    return "\n".join(lines) if lines else f"没有匹配 {pattern} 的文件"


def write_file(path: str, content: str, overwrite: bool = False) -> str:
    p = safe_path(path)
    if p.exists():
        if p.is_dir():
            raise ValueError(f"目标是一个目录，不能当文件写：{path}")
        if not overwrite:
            raise ValueError(f"目标已存在，拒绝覆盖（需要 overwrite=true）：{path}")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {rel(p)}（{len(content)} 字符）"


def describe_write_v4(name: str, args: dict) -> str:
    """写操作的计划文本。write_file 额外显示内容预览，避免用户盲签。"""
    if name != "write_file":
        return describe_write(name, args)

    content = args.get("content") or ""
    preview = content.replace("\n", "\\n").replace("\r", "")
    if len(preview) > 40:
        preview = preview[:40] + "…"
    try:
        exists = safe_path(args.get("path", "")).exists()
    except Exception:
        exists = False
    extra = "，覆盖已存在文件" if exists else ""
    return f"写入 workspace/{args.get('path')}（{len(content)} 字符{extra}）\n         预览：{preview}"


def run_tool(name: str, args: dict, ask=None, log=None) -> str:
    """分发 6 个工具。写工具先打印计划并等 y；任何错误都变成文字回给模型。"""
    registry = {
        "list_dir": list_dir,
        "read_file": read_file,
        "mkdir": mkdir,
        "move_file": move_file,
        "search_files": search_files,
        "write_file": write_file,
    }
    fn = registry.get(name)
    if fn is None:
        return f"没有名为 {name} 的工具，可用：{', '.join(registry)}"

    try:
        if name in WRITE_TOOLS:
            # 覆盖目标却没说 overwrite 时，先如实报错，别让用户白按一次 y。
            if name == "write_file":
                target = safe_path(args.get("path", ""))
                if target.exists() and not args.get("overwrite"):
                    return f"工具执行失败：目标已存在，拒绝覆盖（需要 overwrite=true）：{args.get('path')}"

            plan = describe_write_v4(name, args)
            print(f"[计划] {plan}")
            if log:
                log("write_plan", tool=name, args=summarize_args(name, args))
            ask = ask or input
            try:
                answer = ask("执行? (y/N) ")
            except EOFError:
                answer = ""
            approved = answer.strip().lower() == "y"
            if log:
                log("write_confirm", tool=name, approved=approved)
            if not approved:
                return "用户拒绝了这次操作，未执行。"

        return fn(**args)
    except Exception as exc:
        return f"工具执行失败：{exc}"
