# -*- coding: utf-8 -*-
"""维护者工具: 把整个项目目录安装(镜像)到 agent 的 skills 目录。

本项目的**项目根目录本身**就是可分发的 skill 包, agent 通过 skills 目录
发现它。改完代码后执行本脚本, 即可让注册副本与项目保持一致。

用法:
    python -X utf8 tests/install_skill.py                 # 默认装到用户 skills 目录
    python -X utf8 tests/install_skill.py --dest <目录>   # 指定目标(便于测试)
    python -X utf8 tests/install_skill.py --check         # 只比较是否一致

目标目录默认取 `Path.home()/.workbuddy/skills/ai-drawing-executor`,
不写死任何盘符或用户名。排除规则见 `tests/skill_dist.py`
(本机记忆、运行产物、config.json、.python-path、各类缓存不参与分发)。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import skill_dist  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = Path.home() / ".workbuddy" / "skills" / "ai-drawing-executor"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="安装 AI 绘画 skill 到 skills 目录")
    parser.add_argument("--dest", default=str(DEFAULT_DEST),
                        help=f"目标目录(默认 {DEFAULT_DEST})")
    parser.add_argument("--check", action="store_true",
                        help="只检查是否一致, 不写入")
    args = parser.parse_args(argv)
    dest = Path(args.dest).expanduser()

    if args.check:
        if not dest.is_dir():
            print(f"[FAIL] 目标不存在: {dest}")
            return 1
        d = skill_dist.diff(ROOT, dest)
        if d:
            print(f"[FAIL] 注册副本与项目不一致 ({len(d)} 处):")
            for x in d[:20]:
                print("   -", x)
            print("请运行: python -X utf8 tests/install_skill.py")
            return 1
        print(f"[OK] 注册副本与项目一致: {dest}")
        return 0

    files = skill_dist.mirror(ROOT, dest)
    print(f"已安装 {len(files)} 个文件 -> {dest}")
    for rel in files:
        print(f"    {rel}  ({(dest / rel).stat().st_size} B)")
    if skill_dist.diff(ROOT, dest):
        print("[WARN] 镜像后仍检测到差异, 请检查权限")
        return 1
    print("镜像完成且校验一致 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
