#!/usr/bin/env python3
"""检查仓库里有没有和 Python 标准库同名的模块/包。

为什么:直接执行 `python3 some-dir/x_test.py` 时,Python 会把 some-dir/ 插到
sys.path 最前面。若该目录里有 operator.py / queue.py / types.py 之类与标准库同名
的文件,标准库内部的 import(例如 collections 里的 `from operator import ...`)会
先命中项目文件,连 `import json` 都会失败。容器里用 `python -m pkg.mod` 启动时不会
触发,所以这类问题平时很难发现(sandbox-api/operator.py 曾因此让本地单测全部导入失败)。

用法:
  python3 scripts/check_stdlib_shadowing.py          # 扫描整个仓库,有冲突则退出码 1
  python3 scripts/check_stdlib_shadowing.py a.py b/  # 只检查给定文件/目录

Python >= 3.10 用 sys.stdlib_module_names;更低版本扫描标准库目录。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".terraform", ".venv", "venv",
             "build", "dist", ".next", "bench-results"}


def stdlib_names() -> frozenset[str]:
    names = getattr(sys, "stdlib_module_names", None)  # Python >= 3.10
    if names is not None:
        return frozenset(names)
    # Python < 3.10:扫描标准库目录 + 内置模块,保证任何版本都不跳过检查
    import sysconfig
    found = set(sys.builtin_module_names)
    for key in ("stdlib", "platstdlib"):
        root = Path(sysconfig.get_paths()[key])
        for child in root.iterdir() if root.is_dir() else ():
            if child.name in ("site-packages", "dist-packages", "test", "tests"):
                continue
            if child.suffix == ".py":
                found.add(child.stem)
            elif child.is_dir() and (child / "__init__.py").exists():
                found.add(child.name)
        dyn = root / "lib-dynload"
        for child in dyn.iterdir() if dyn.is_dir() else ():
            found.add(child.name.split(".", 1)[0])
    return frozenset(found)


def module_name(path: Path) -> str | None:
    if path.is_file() and path.suffix == ".py":
        return path.stem
    if path.is_dir() and (path / "__init__.py").exists():
        return path.name
    return None


def iter_candidates(roots: list[Path]):
    for root in roots:
        if root.is_file():
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            base = Path(dirpath)
            for d in dirnames:
                yield base / d
            for f in filenames:
                if f.endswith(".py"):
                    yield base / f


def main(argv: list[str]) -> int:
    repo = Path(__file__).resolve().parent.parent
    roots = [Path(a) for a in argv] or [repo]
    std = stdlib_names()
    hits = []
    for path in iter_candidates(roots):
        if path.is_symlink():
            continue  # 例如 sandbox_api -> sandbox-api,避免重复
        name = module_name(path)
        if name and name in std and name != "__init__":
            hits.append(path)
    for path in sorted(set(hits)):
        try:
            shown = path.resolve().relative_to(repo)
        except ValueError:
            shown = path
        print(f"✗ {shown}: 与标准库模块 '{module_name(path)}' 同名,请改名", file=sys.stderr)
    if hits:
        print("  直接执行同目录脚本(python3 dir/x.py)时会遮蔽标准库,导致导入失败。",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
