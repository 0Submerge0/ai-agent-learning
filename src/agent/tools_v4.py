"""V4 工具集：V3 的 4 个工具 + search_files + write_file，带日志钩子。

════════════════════════════════════════════════════════════
【它和 tools.py 是什么关系】
tools.py 是 V3 的 4 个工具。这个文件做「加法」，不复制粘贴：

    TOOLS_V4 = TOOLS_V3 + [search_files, write_file]

所以这个文件里没有 list_dir / mkdir / move_file 的实现，
它们是从 tools.py 顶部的 from .tools import 拿过来的。

【V4 加的两个工具，各自解决什么】
    search_files  没有它，模型只能一个目录一个目录地 list_dir 找文件，
                  7 个文件能耗掉 7 轮。有了它一轮搞定。
    write_file    没有它，模型没法生成报告。这是 V3 完全缺失的能力。

【这个文件最值得看的三处】
1. summarize_args：写进日志时要把内容截断（内容可能几 KB）
2. describe_write_v4：write_file 要显示内容预览，不能让用户盲签
3. write_file 分支：在问 y 之前先检查「覆盖却没声明 overwrite」——
   避免用户按了 y 之后才发现根本执行不了

【读完这个文件你应该能回答】
1. 为什么 write_file 需要 overwrite 参数，而 move_file 是直接拒绝覆盖？
2. summarize_args 存在的理由是什么？（日志里不能塞全文）
3. write_plan 和 write_confirm 两条日志分开发，有什么好处？
4. 为什么 run_tool 要单独维护一份 registry，不能用 globals()？
════════════════════════════════════════════════════════════
"""

from pathlib import Path

# 从 V3 的模块里取：路径校验、显示转换、以及 4 个现成工具
from .sandbox import WORKSPACE, rel, safe_path
from .tools import TOOLS as TOOLS_V3
from .tools import WRITE_TOOLS as WRITE_TOOLS_V3
from .tools import describe_write, list_dir, mkdir, move_file, read_file

# ══════════════════════════════════════════════════════════════
# 工具清单：V3 的 4 个 + 新增 2 个
# ══════════════════════════════════════════════════════════════
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
            "required": ["pattern"],  # path 可选，不传就从根目录搜
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
            "required": ["path", "content"],  # overwrite 可选
        },
    },
]

# 集合并集：V3 的两个 + write_file。
# 注意 search_files 不在里面——它只读，不该弹确认。
WRITE_TOOLS = WRITE_TOOLS_V3 | {"write_file"}


def summarize_args(name: str, args: dict) -> dict:
    """日志里只记参数的关键部分：write_file 的内容可能很长，也可能含敏感信息。

    为什么不直接记 args？
    因为 write_file 的 content 可能有几千字符，写进日志会：
      1. 日志文件爆炸
      2. 万一内容是密码、token、个人隐私，就永久留在磁盘上了
    所以对 write_file 只记「长度」，不记内容本身。

    而这一点对排查问题够用：知道写了多少字符，就能判断是不是写空了。
    """
    if name == "write_file":
        return {
            "path": args.get("path"),
            "chars": len(args.get("content") or ""),
            "overwrite": bool(args.get("overwrite")),
        }
    # 其他工具的参数都很短，原样记
    return dict(args)


def search_files(pattern: str, path: str = ".") -> str:
    """按文件名模式递归搜索。只读，不需要确认。

    实现用 Path.rglob()，支持 *.jpg、发票* 这类通配。
    """
    pattern = (pattern or "").strip()
    if not pattern:
        raise ValueError("搜索模式不能为空")

    # 模式只允许是「文件名模式」，不能借它跳出 workspace。
    # 这是很容易漏的一个口子：如果有人传 pattern="../../*"，
    # rglob 会在 workspace 之外遍历。
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
        # 这一层不能省：rglob 会跟进符号链接，而 safe_path 只校验了起点。
        if not target.is_relative_to(root):
            continue
        hits.append(entry)
        # 边搜边数：命中 50 个就停，避免在大目录上遍历几十万文件。
        if len(hits) > 50:
            break

    # 排序是为了输出稳定，模型每次看到的顺序一致，推理更稳。
    hits.sort(key=lambda e: rel(e).lower())
    lines = []
    for entry in hits[:50]:
        lines.append(f"{rel(entry)}/" if entry.is_dir() else f"{rel(entry)}  {entry.stat().st_size} B")
    if len(hits) > 50:
        lines.append("（已截断，只显示前 50 条）")
    return "\n".join(lines) if lines else f"没有匹配 {pattern} 的文件"


def write_file(path: str, content: str, overwrite: bool = False) -> str:
    """写文本文件。写操作，会走确认闸门。

    返回的字符串会被当成 tool_result 喂回模型，
    所以里面写了「多少字符」——让模型能核对是不是写对了。
    """
    p = safe_path(path)
    if p.exists():
        if p.is_dir():
            # 目录不能当文件写，这是模型常犯的错，错误信息要具体
            raise ValueError(f"目标是一个目录，不能当文件写：{path}")
        if not overwrite:
            # 默认拒绝覆盖。要覆盖必须显式传 overwrite=true，
            # 这个「显式」会出现在给用户看的计划文本里，让用户看见风险。
            raise ValueError(f"目标已存在，拒绝覆盖（需要 overwrite=true）：{path}")
    # 目标可能在 images/ 这种还不存在的目录里，先把父目录建出来
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {rel(p)}（{len(content)} 字符）"


def describe_write_v4(name: str, args: dict) -> str:
    """写操作的计划文本。write_file 额外显示内容预览，避免用户盲签。

    为什么要预览？
    移动文件，用户看路径就知道后果。
    写文件，用户不知道里面是什么——可能是一份报告，也可能是一堆乱码。
    不预览的话，按 y 就是闭着眼睛签字。
    """
    if name != "write_file":
        # 其他工具交给 V3 的实现处理
        return describe_write(name, args)

    content = args.get("content") or ""
    # 把换行转成字面 \n，否则预览会变成多行，把终端刷屏
    preview = content.replace("\n", "\\n").replace("\r", "")
    if len(preview) > 40:
        preview = preview[:40] + "…"  # 只给前 40 字符，够判断意图
    try:
        # 提前查一下目标在不在，好告诉用户「这是覆盖，不是新建」
        exists = safe_path(args.get("path", "")).exists()
    except Exception:
        # 路径本身非法就不用管了，后面 fn(**args) 会报错
        exists = False
    extra = "，覆盖已存在文件" if exists else ""
    return f"写入 workspace/{args.get('path')}（{len(content)} 字符{extra}）\n         预览：{preview}"


def run_tool(name: str, args: dict, ask=None, log=None) -> str:
    """分发 6 个工具。写工具先打印计划并等 y；任何错误都变成文字回给模型。

    和 V3 的 run_tool 相比多了三样：
      log 参数          记 write_plan / write_confirm 两条日志
      write_file 分支   覆盖检查提前做
      describe_write_v4 write_file 的预览
    """
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
        # ── 确认闸门 ────────────────────────────────────────
        if name in WRITE_TOOLS:
            # 覆盖目标却没说 overwrite 时，先如实报错，别让用户白按一次 y。
            # 这个检查放在问 y 之前是有讲究的：
            # 如果放后面，用户会先看到「写入 report.md」按了 y，
            # 然后才收到「目标已存在，拒绝覆盖」——白按一次，还容易误解。
            if name == "write_file":
                target = safe_path(args.get("path", ""))
                if target.exists() and not args.get("overwrite"):
                    return f"工具执行失败：目标已存在，拒绝覆盖（需要 overwrite=true）：{args.get('path')}"

            plan = describe_write_v4(name, args)
            print(f"[计划] {plan}")
            # 记「我们打算做什么」。注意是打算，不是做完了。
            if log:
                log("write_plan", tool=name, args=summarize_args(name, args))

            ask = ask or input
            try:
                answer = ask("执行? (y/N) ")
            except EOFError:
                answer = ""
            approved = answer.strip().lower() == "y"

            # 单独记一条「用户批没批」。
            # 分开记的好处：事后能还原「用户看没看见这一步」，
            # 而不只是知道「最后有没有执行」。
            if log:
                log("write_confirm", tool=name, approved=approved)

            if not approved:
                return "用户拒绝了这次操作，未执行。"

        return fn(**args)
    except Exception as exc:
        return f"工具执行失败：{exc}"
