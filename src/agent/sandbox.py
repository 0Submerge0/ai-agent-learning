"""沙箱：所有文件路径必须落在 workspace 之内。"""

from pathlib import Path

# 相对本文件自身定位，而不是写死 D:\...，换机器/换目录也不会失效。
WORKSPACE = Path(__file__).resolve().parent.parent.parent / "workspace"


def safe_path(path: str, must_exist: bool = False) -> Path:
    """把模型给的路径解析成绝对路径，并确认它没跑出 workspace。

    resolve() 会展开 .. 和符号链接，所以越界会先被折回真实位置，再被前缀检查拦住。
    """
    base = WORKSPACE.resolve()
    p = Path(path)
    target = (p if p.is_absolute() else base / p).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"路径越界，只允许操作 workspace 之内：{path}")
    if must_exist and not target.exists():
        raise ValueError(f"路径不存在：{path}")
    return target


def rel(p: Path) -> str:
    """把绝对路径转成相对 workspace 的显示形式，如 images/a.jpg。"""
    try:
        return p.resolve().relative_to(WORKSPACE.resolve()).as_posix()
    except ValueError:
        return p.as_posix()
