# -*- coding: utf-8 -*-
"""分发/安装本 skill 时共用的排除规则与镜像函数。

`tests/test_skill_standalone.py`(验证可移植性)与 `tests/install_skill.py`
(安装到 harness 的 skills 目录)共用这里的口径, 避免两处规则漂移。

分发单元就是**项目根目录本身**; 但下面这些属于本机运行态或私有配置,
不应随分发包带走:
  - `.workbuddy/`  项目记忆(本机私有)
  - `out/`         运行产物
  - `config.json`  本机配置, 可能含模型 api_key
  - `.python-path` 本机解释器路径
  - 各类缓存与虚拟环境
"""
import filecmp
import shutil
from pathlib import Path

EXCLUDE_DIRS = {".workbuddy", "out", ".git", "__pycache__", ".venv",
                ".idea", ".vscode", ".pytest_cache", "node_modules",
                ".ai-draw-session"}
EXCLUDE_FILES = {"config.json", ".python-path", "config.json.bak",
                 "MISSION.md", "demo_drawing.png", "demo_gui_export.png"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log"}


def is_excluded(rel: Path) -> bool:
    """rel 为相对项目根的路径; 返回是否应排除。"""
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if rel.name in EXCLUDE_FILES:
        return True
    return rel.suffix.lower() in EXCLUDE_SUFFIXES


def iter_dist_files(src: Path):
    """产出应被分发的文件(相对路径), 已排序。"""
    out = []
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(src)
        if not is_excluded(rel):
            out.append(rel)
    return sorted(out)


def mirror(src: Path, dst: Path) -> list:
    """把 src 按分发口径镜像到 dst(先清空 dst, 保留平台元数据)。返回文件列表。"""
    if dst.is_dir():
        for p in sorted(dst.iterdir()):
            if p.name == "_user_meta.json":   # 平台写入的元数据, 保留
                continue
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    dst.mkdir(parents=True, exist_ok=True)
    for rel in iter_dist_files(src):
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, target)
    return iter_dist_files(src)


def diff(src: Path, dst: Path) -> list:
    """按分发口径比较, 返回不一致的相对路径列表。"""
    out = []
    for rel in iter_dist_files(src):
        q = dst / rel
        if not q.is_file() or not filecmp.cmp(src / rel, q, shallow=False):
            out.append(str(rel) + ("" if q.is_file() else "  (缺失)"))
    return out
