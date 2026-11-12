"""沙箱：所有文件路径必须落在 workspace 之内。

════════════════════════════════════════════════════════════
【这个文件只有 29 行，但它是整个项目的安全底线】
模型会给出任意路径——包括 "../../Windows/System32" 这种。
safe_path() 是唯一的守门人：任何工具动文件之前都必须先过它。

【设计文档里不可协商的一条】
「文件类工具只允许操作 workspace 之下；路径先 resolve 再校验前缀，
  拒绝 .. 逃逸与符号链接穿透。」

【读完这个文件你应该能回答】
1. 为什么先 resolve() 再 is_relative_to()？反过来会怎样？
2. 符号链接攻击是怎么被挡住的？
3. 为什么 WORKSPACE 用 __file__ 算，而不是写死 D:\\...？
════════════════════════════════════════════════════════════
"""

from pathlib import Path

# 相对本文件自身定位，而不是写死 D:\...，换机器/换目录也不会失效。
# __file__      = .../src/agent/sandbox.py
# .parent       = src/agent
# .parent.parent= src
# .parent.parent.parent = 项目根目录
WORKSPACE = Path(__file__).resolve().parent.parent.parent / "workspace"


def safe_path(path: str, must_exist: bool = False) -> Path:
    """把模型给的路径解析成绝对路径，并确认它没跑出 workspace。

    resolve() 会展开 .. 和符号链接，所以越界会先被折回真实位置，再被前缀检查拦住。

    【为什么顺序不能反】
    如果先做字符串前缀检查、再 resolve，那么
        workspace/link -> C:\\Windows
    会在检查时看起来「以 workspace 开头」，通过检查；
    但真正打开文件时 resolve 才知道它指向 C:\\Windows。
    先 resolve 就相当于「先把门打开看清楚里面是谁，再决定放不放行」。

    参数：
        path        模型给的路径，可能是相对路径（相对 workspace）或绝对路径
        must_exist  为 True 时不存在的路径直接报错
                    （读操作要 True；创建类操作要 False，因为目标本来就还不存在）
    返回：绝对路径 Path 对象
    """
    base = WORKSPACE.resolve()

    p = Path(path)
    # 绝对路径就原样用，相对路径就拼在 workspace 下面。
    target = (p if p.is_absolute() else base / p).resolve()

    # 核心检查：解析后的真实路径必须在 workspace 之内。
    # is_relative_to 是 Python 3.9+ 的方法，比字符串 startswith 安全——
    # 它按路径段比较，workspace_evil 不会被误判成 workspace 的子目录。
    if not target.is_relative_to(base):
        raise ValueError(f"路径越界，只允许操作 workspace 之内：{path}")

    # 存在性检查放在越界检查之后：先确认安全,再看在不在。
    if must_exist and not target.exists():
        raise ValueError(f"路径不存在：{path}")

    return target


def rel(p: Path) -> str:
    """把绝对路径转成相对 workspace 的显示形式，如 images/a.jpg。

    为什么要转换？两个原因：
    1. 打印给用户看时，D:\\zjy\\AI\\workspace\\images\\a.jpg 太长；
    2. 喂给模型的文本越短越省 token。

    as_posix() 把 Windows 的反斜杠统一成正斜杠，
    避免模型看到 \"images\\\\a.jpg\" 这种转义后的路径而搞错。
    """
    try:
        return p.resolve().relative_to(WORKSPACE.resolve()).as_posix()
    except ValueError:
        # 不在 workspace 里的路径（理论上不该出现），原样返回便于排查。
        return p.as_posix()
