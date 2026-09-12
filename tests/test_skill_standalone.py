# -*- coding: utf-8 -*-
"""分发包的独立性与可移植性测试。

核心命题: **项目根目录就是分发单元** —— 整个目录拷到任何地方、在任何工作
目录下启动都必须能跑, 不写死任何绝对路径, 也不依赖项目之外的任何东西。

覆盖:
- 根目录结构完整(SKILL.md / 桥 / 自检 / 观察窗 / 依赖清单), 旧的 skill/ 已移除
- 根目录入口脚本无绝对路径字面量、不向上推算路径、不依赖已删除的内联副本
- 整个项目拷贝到项目之外的临时目录后:
    * stdio 桥可从无关 cwd 作画
    * selfcheck 全通过
    * watch_viewer 的纯函数可导入
- 安装到 harness 的注册副本与项目一致(副本不存在时跳过)
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import skill_dist  # noqa: E402

PASS = 0
FAIL = 0

# 分发单元必须自带的根级文件
ROOT_FILES = ["SKILL.md", "bridge_stdio.py", "selfcheck.py",
              "watch_viewer.py", "requirements.txt"]
# 入口脚本(不含生成物)
ENTRY_SCRIPTS = ["bridge_stdio.py", "selfcheck.py", "watch_viewer.py"]


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}", flush=True)
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}", flush=True)


def run(cmd, cwd, stdin=None, timeout=300):
    return subprocess.run(cmd, cwd=str(cwd), input=stdin, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


# --------------------------------------------------------------------------
def test_root_layout():
    print("== 根目录即分发包 ==")
    for n in ROOT_FILES:
        check(f"根目录存在 {n}", (ROOT / n).is_file())
    check("app 包在根目录内", (ROOT / "app" / "skill.py").is_file())
    check("旧的 skill/ 子目录已移除", not (ROOT / "skill").exists())
    check("不再有内联副本 aidraw/", not (ROOT / "aidraw").exists())
    check("不再需要副本生成器",
          not (ROOT / "tests" / "sync_skill_core.py").exists())
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    check("依赖清单含 numpy 与 pillow",
          "numpy" in req and "pillow" in req, req.replace("\n", " "))
    skillmd = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    check("SKILL.md 不再引用 skill/ 子目录", "skill/bridge_stdio.py" not in skillmd)
    check("SKILL.md 记录了可移植说明", "目录即分发包" in skillmd)
    check("SKILL.md 写明作画节奏(一笔一笔画, 画几笔看一眼)",
          "一笔一笔画" in skillmd and "get_current_picture" in skillmd)


def test_no_absolute_paths():
    print("== 入口脚本无绝对路径 ==")
    drive = re.compile(r"[A-Za-z]:[\\/]")
    bad = []
    for n in ENTRY_SCRIPTS:
        text = (ROOT / n).read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if drive.search(line) or "/home/" in line or "/Users/" in line:
                bad.append(f"{n}:{i} {line.strip()[:80]}")
    check("入口脚本无绝对路径字面量", not bad, "; ".join(bad)[:300])

    bridge = (ROOT / "bridge_stdio.py").read_text(encoding="utf-8")
    check("桥只把自身目录加入 sys.path(不向上推算项目结构)",
          "Path(__file__).resolve().parent)" in bridge
          and "parent.parent" not in bridge)
    check("桥通过公共入口 app.skill 调用引擎",
          "from app.skill import" in bridge and "import aidraw" not in bridge)

    vb = (ROOT / "run.bat").read_text(encoding="utf-8")
    check("run.bat 不写死解释器绝对路径", not drive.search(vb))
    check("run.bat 提供可移植的查找顺序",
          "AI_DRAWING_PYTHON" in vb and ".python-path" in vb
          and ".venv" in vb)


def test_relocated_project_runs():
    print("== 整个目录拷到项目之外运行(可移植性) ==")
    with tempfile.TemporaryDirectory(prefix="aidraw_portable_") as tmp:
        tmp_path = Path(tmp)
        dst = tmp_path / "ai-drawing"      # 模拟拷到别人电脑
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        copied = skill_dist.mirror(ROOT, dst)
        check("按分发口径拷贝成功", len(copied) > 10, f"{len(copied)} 个文件")
        check("拷贝不带本机私有配置",
              not (dst / "config.json").exists()
              and not (dst / ".python-path").exists()
              and not (dst / ".workbuddy").exists()
              and not (dst / "out").exists())
        check("拷贝后结构完整",
              (dst / "SKILL.md").is_file() and (dst / "app" / "skill.py").is_file()
              and (dst / "requirements.txt").is_file())

        # 从项目之外、且完全无关的工作目录启动桥
        lines = [
            json.dumps({"command": "ping"}),
            json.dumps({"command": "init", "width": 900, "height": 700}),
            json.dumps({"id": 1, "tool": "pick_tools",
                        "arguments": {"tool": "pen",
                                      "settings": {"size": 30,
                                                   "color": "#cc0000"}}}),
            json.dumps({"id": 2, "tool": "use_tool",
                        "arguments": {"mode": "path", "path": {
                            "shape": "circle",
                            "params": {"cx": 500, "cy": 500, "r": 250}}}}),
            json.dumps({"id": 3, "tool": "get_current_picture",
                        "arguments": {}}),
            json.dumps({"command": "get_tools_schema"}),
            json.dumps({"command": "shutdown"}),
        ]
        r = run([sys.executable, "-X", "utf8", str(dst / "bridge_stdio.py")],
                cwd=elsewhere, stdin="\n".join(lines) + "\n")
        check("桥在项目外启动且干净退出", r.returncode == 0,
              f"rc={r.returncode} stderr={r.stderr.strip()[-200:]}")
        resp = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
        check("桥返回 7 条合法响应", len(resp) == 7, f"{len(resp)} 条")
        check("ping 正常", any(x.get("pong") for x in resp))
        draw = next((x for x in resp if x.get("id") == 2), {})
        check("项目外作画成功", draw.get("result", {}).get("ok") is True
              and draw.get("result", {}).get("changed") is True)
        pic = next((x for x in resp if x.get("id") == 3), {})
        check("项目外取回画布图片",
              bool(pic.get("result", {}).get("base64_image")))
        sch = next((x for x in resp if x.get("tools")), {})
        check("项目外工具集完整(8 个)", len(sch.get("tools", [])) == 8)

        # 自检脚本同样要在项目外通过
        r = run([sys.executable, "-X", "utf8", str(dst / "selfcheck.py"),
                 "--out", str(elsewhere / "selfcheck.png")], cwd=elsewhere)
        check("selfcheck 在项目外全通过", r.returncode == 0,
              (r.stdout + r.stderr).strip()[-300:])
        check("selfcheck 产物落盘", (elsewhere / "selfcheck.png").is_file())

        # 观察窗的纯函数(不依赖 GUI)也应可用
        r = run([sys.executable, "-c",
                 "import sys;sys.path.insert(0,sys.argv[1]);"
                 "from watch_viewer import load_frame_png, available_backends;"
                 "print('ok', load_frame_png('nope.png'), available_backends())",
                 str(dst)], cwd=elsewhere)
        check("观察窗模块在项目外可导入", r.returncode == 0
              and "ok None" in r.stdout, (r.stdout + r.stderr).strip()[-200:])


def test_installed_copy_in_sync():
    print("== 已安装副本一致性 ==")
    dest = Path.home() / ".workbuddy" / "skills" / "ai-drawing-executor"
    if not dest.is_dir():
        print("  [skip] 尚未安装到 skills 目录, 跳过")
        return
    d = skill_dist.diff(ROOT, dest)
    check("注册副本与项目一致", not d,
          f"{len(d)} 处不一致: {d[:4]}")


def main():
    print("分发单元独立性与可移植性测试")
    print("=" * 60)
    test_root_layout()
    test_no_absolute_paths()
    test_relocated_project_runs()
    test_installed_copy_in_sync()
    print("=" * 60)
    print(f"独立性测试: {PASS} 通过, {FAIL} 失败")
    if FAIL:
        sys.exit(1)
    print("DISTRIBUTABLE FOLDER IS SELF-CONTAINED AND PORTABLE")


if __name__ == "__main__":
    main()
